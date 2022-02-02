# Copyright 2019, The TensorFlow Federated Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# pytype: skip-file
# This modules disables the Pytype analyzer, see
# https://github.com/tensorflow/federated/blob/main/docs/pytype.md for more
# information.
"""A context for execution based on an embedded executor instance."""

import asyncio
import concurrent
import contextlib
import pprint
import textwrap
from typing import Any, Callable, Optional

from absl import logging
import tensorflow as tf

from pybind11_abseil import status as absl_status
from tensorflow_federated.python.common_libs import py_typecheck
from tensorflow_federated.python.common_libs import retrying
from tensorflow_federated.python.common_libs import structure
from tensorflow_federated.python.common_libs import tracing
from tensorflow_federated.python.core.api import computation_base
from tensorflow_federated.python.core.impl.compiler import compiler_pipeline
from tensorflow_federated.python.core.impl.context_stack import context_base
from tensorflow_federated.python.core.impl.execution_contexts import async_execution_context
from tensorflow_federated.python.core.impl.executors import cardinalities_utils
from tensorflow_federated.python.core.impl.executors import executor_base
from tensorflow_federated.python.core.impl.executors import executor_factory
from tensorflow_federated.python.core.impl.executors import executor_value_base
from tensorflow_federated.python.core.impl.executors import executors_errors
from tensorflow_federated.python.core.impl.executors import ingestable_base
from tensorflow_federated.python.core.impl.executors import value_serialization
from tensorflow_federated.python.core.impl.types import computation_types
from tensorflow_federated.python.core.impl.types import type_conversions
from tensorflow_federated.python.core.impl.types import typed_object


def _unwrap(value):
  if isinstance(value, tf.Tensor):
    return value.numpy()
  elif isinstance(value, structure.Struct):
    return structure.Struct(
        (k, _unwrap(v)) for k, v in structure.iter_elements(value))
  elif isinstance(value, list):
    return [_unwrap(v) for v in value]
  else:
    return value


def _is_retryable_error(exception):
  return isinstance(exception, executors_errors.RetryableError)


class AsyncExecutionContextValue(typed_object.TypedObject):
  """Wrapper class for values produced by `ExecutionContext`."""

  def __init__(self, value, type_spec):
    py_typecheck.check_type(type_spec, computation_types.Type)
    self._value = value
    self._type_spec = type_spec

  @property
  def type_signature(self):
    return self._type_spec

  @property
  def value(self):
    return self._value


async def _ingest(executor, val, type_spec):
  """A coroutine that handles ingestion.

  Args:
    executor: An instance of `executor_base.Executor`.
    val: The first argument to `context_base.Context.ingest()`.
    type_spec: The second argument to `context_base.Context.ingest()`.

  Returns:
    The result of the ingestion.

  Raises:
    TypeError: If the `val` is not a value of type `type_spec`.
  """
  if isinstance(val, executor_value_base.ExecutorValue):
    return val
  elif isinstance(val, ingestable_base.Ingestable):
    val_type = val.type_signature
    py_typecheck.check_type(val_type, computation_types.Type)
    type_spec.check_assignable_from(val_type)
    return await val.ingest(executor)
  elif (isinstance(val, structure.Struct) and not type_spec.is_federated()):
    type_spec.check_struct()
    v_elem = structure.to_elements(val)
    t_elem = structure.to_elements(type_spec)
    if len(v_elem) != len(t_elem):
      raise TypeError(
          'Value {} does not match type {}: mismatching tuple length.'.format(
              val, type_spec))
    for ((vk, _), (tk, _)) in zip(v_elem, t_elem):
      if vk not in [tk, None]:
        raise TypeError(
            'Value {} does not match type {}: mismatching tuple element '
            'names {} vs. {}.'.format(val, type_spec, vk, tk))
    ingested = []
    for (_, v), (_, t) in zip(v_elem, t_elem):
      ingested.append(_ingest(executor, v, t))
    ingested = await asyncio.gather(*ingested)
    return await executor.create_struct(
        structure.Struct(
            (name, val) for (name, _), val in zip(t_elem, ingested)))
  else:
    return await executor.create_value(val, type_spec)


async def _invoke(executor, comp, arg, result_type: computation_types.Type):
  """A coroutine that handles invocation.

  Args:
    executor: An instance of `executor_base.Executor`.
    comp: The first argument to `context_base.Context.invoke()`.
    arg: The optional second argument to `context_base.Context.invoke()`.
    result_type: The type signature of the result. This is used to convert the
      execution result into the proper container types.

  Returns:
    The result of the invocation.
  """
  if arg is not None:
    py_typecheck.check_type(arg, executor_value_base.ExecutorValue)
  comp = await executor.create_value(comp)
  result = await executor.create_call(comp, arg)
  py_typecheck.check_type(result, executor_value_base.ExecutorValue)
  result_val = _unwrap(await result.compute())
  return type_conversions.type_to_py_container(result_val, result_type)


def _unwrap_execution_context_value(val):
  """Recursively removes wrapping from `val` under anonymous tuples."""
  if isinstance(val, structure.Struct):
    value_elements_iter = structure.iter_elements(val)
    return structure.Struct((name, _unwrap_execution_context_value(elem))
                            for name, elem in value_elements_iter)
  elif isinstance(val, AsyncExecutionContextValue):
    return _unwrap_execution_context_value(val.value)
  else:
    return val


class SingleCardinalityAsyncContext(context_base.Context):
  """Implements shared logic to ensure instance-consistent cardinalities.

  This context holds a stack which represents ongoing computations, as well as
  an integer representing the cardinalities of these computations. An
  asynchronous context manager is used to control access to the underlying
  executor factory, ensuring that computations which are of the same
  cardinalities can run concurrently, but computations of different
  cardinalities must wait until all existing invocations are finished.
  Therefore the concurrency mechanisms notify under two conditions: an
  invocation has completed (so the invocation stack may be empty, and thus
  computations with new cardinalities may be executed) or cardinalities have
  changed (so that a computation which could not run before due to conflicting
  cardinalities with the underway invocations may now run).
  """

  def __init__(self):
    # Delay instantiation of the cardinalities condition to allow it to be
    # instantiated only when an event loop is set to running.
    self._cardinalities_condition = None
    self._invocation_stack = []
    self._current_cardinalities = None

  @property
  def cardinalities_condition(self):
    if self._cardinalities_condition is None:
      self._cardinalities_condition = asyncio.Condition()
    return self._cardinalities_condition

  async def _acquire_and_set_cardinalities(self, cardinalities):
    async with self.cardinalities_condition:
      if self._current_cardinalities == cardinalities:
        return
      else:
        # We need to change the cardinalities; wait until all the underway
        # invocations are finished or the current_cardinalities value matches
        # ours.

        def _condition():
          return (not self._invocation_stack or
                  self._current_cardinalities == cardinalities)

        await self.cardinalities_condition.wait_for(_condition)

        if self._current_cardinalities == cardinalities:
          # No need to update.
          return
        self._current_cardinalities = cardinalities
        # Notify other waiting tasks that we have changed cardinalities, and
        # they may be interested in waking back up.
        self.cardinalities_condition.notify_all()

  @contextlib.asynccontextmanager
  async def _reset_factory_on_error(self, ex_factory, cardinalities):
    try:
      await self._acquire_and_set_cardinalities(cardinalities)
      # There is now invocation underway; add to the stack.
      self._invocation_stack.append(1)
      # We pass a copy down to prevent the caller from mutating.
      yield ex_factory.create_executor({**cardinalities})
    except Exception:
      ex_factory.clean_up_executors()
      raise
    finally:
      self._invocation_stack.pop()
      async with self.cardinalities_condition:
        # Notify all the wait_fors that their conditions to execute might be
        # True, since we popped from the invocation stack.
        self.cardinalities_condition.notify_all()


class AsyncExecutionContext(SingleCardinalityAsyncContext):
  """An asynchronous execution context backed by an `executor_base.Executor`.

  This context's `ingest` and `invoke` methods return Python coroutine objects
  which represent the actual work of ingestion and invocation in the backing
  executor.

  This context will support concurrent invocation of multiple computations if
  their arguments have the same cardinalities.
  """

  def __init__(self,
               executor_fn: executor_factory.ExecutorFactory,
               compiler_fn: Optional[Callable[[computation_base.Computation],
                                              Any]] = None):
    """Initializes an execution context.

    Args:
      executor_fn: Instance of `executor_factory.ExecutorFactory`.
      compiler_fn: A Python function that will be used to compile a computation.
    """
    super().__init__()
    py_typecheck.check_type(executor_fn, executor_factory.ExecutorFactory)
    self._executor_factory = executor_fn
    if compiler_fn is not None:
      py_typecheck.check_callable(compiler_fn)
      self._compiler_pipeline = compiler_pipeline.CompilerPipeline(compiler_fn)
    else:
      self._compiler_pipeline = None

  async def ingest(self, val, type_spec):
    return AsyncExecutionContextValue(val, type_spec)

  @retrying.retry(
      retry_on_exception_filter=_is_retryable_error,
      wait_max_ms=30 * 1000,
      wait_multiplier=2)
  async def invoke(self, comp, arg):
    if asyncio.iscoroutine(arg):
      # Awaiting if we are passed a coro allows us to install and use the async
      # context in conjunction with ConcreteComputations' implementation of
      # __call__.
      arg = await arg
    comp.type_signature.check_function()
    # Save the type signature before compiling. Compilation currently loses
    # container types, so we must remember them here so that they can be
    # restored in the output.
    result_type = comp.type_signature.result
    if self._compiler_pipeline is not None:
      with tracing.span('ExecutionContext', 'Compile', span=True):
        comp = self._compiler_pipeline.compile(comp)

    with tracing.span('ExecutionContext', 'Invoke', span=True):

      if arg is not None:
        py_typecheck.check_type(arg, AsyncExecutionContextValue)
        unwrapped_arg = _unwrap_execution_context_value(arg)
        cardinalities = cardinalities_utils.infer_cardinalities(
            unwrapped_arg, arg.type_signature)
      else:
        cardinalities = {}

      async with self._reset_factory_on_error(self._executor_factory,
                                              cardinalities) as executor:
        py_typecheck.check_type(executor, executor_base.Executor)

        if arg is not None:
          arg = await tracing.wrap_coroutine_in_current_trace_context(
              _ingest(executor, unwrapped_arg, arg.type_signature))

        return await tracing.wrap_coroutine_in_current_trace_context(
            _invoke(executor, comp, arg, result_type))


# TODO(b/193900393): Define a custom error in CPP and expose to python to
# more easily localize and control retries.
def _is_retryable_absl_status(exception):
  return (isinstance(exception, absl_status.StatusNotOk) and
          exception.status.code() in [absl_status.StatusCode.UNAVAILABLE])


class AsyncSerializeAndExecuteCPPContext(
    async_execution_context.SingleCardinalityAsyncContext):
  """An async execution context delegating to CPP Executor bindings."""

  def __init__(self, factory, compiler_fn, max_workers=None):
    super().__init__()
    self._factory = factory
    self._compiler_pipeline = compiler_pipeline.CompilerPipeline(compiler_fn)
    self._futures_executor_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers)

  async def ingest(self, val, type_spec):
    return val

  @retrying.retry(
      retry_on_exception_filter=_is_retryable_absl_status,
      wait_max_ms=300_000,  # 5 minutes.
      wait_multiplier=2,
  )
  async def invoke(self, comp, arg):
    if asyncio.iscoroutine(arg):
      arg = await arg
    compiled_comp = self._compiler_pipeline.compile(comp)
    serialized_comp, _ = value_serialization.serialize_value(
        compiled_comp, comp.type_signature)
    cardinalities = cardinalities_utils.infer_cardinalities(
        arg, comp.type_signature.parameter)

    try:
      async with self._reset_factory_on_error(self._executor_factory,
                                              cardinalities) as executor:
        fn = executor.create_value(serialized_comp)
        if arg is not None:
          try:
            serialized_arg, _ = value_serialization.serialize_value(
                arg, comp.type_signature.parameter)
          except Exception as e:
            raise TypeError(
                f'Failed to serialize argument:\n{arg}\nas a value of type:\n'
                f'{comp.type_signature.parameter}') from e
          arg_value = executor.create_value(serialized_arg)
          call = executor.create_call(fn.ref, arg_value.ref)
        else:
          call = executor.create_call(fn.ref, None)
        # Delaying grabbing the event loop til now ensures that the call below
        # is attached to the loop running the invoke.
        running_loop = asyncio.get_running_loop()
        result_pb = await running_loop.run_in_executor(
            self._futures_executor_pool, lambda: executor.materialize(call.ref))
    except absl_status.StatusNotOk:
      indent = lambda s: textwrap.indent(s, prefix='\t')
      if arg is None:
        arg_str = 'without any arguments'
      else:
        arg_str = f'with argument:\n{indent(pprint.pformat(arg))}'
      logging.error('Error invoking computation with signature:\n%s\n%s\n',
                    indent(comp.type_signature.formatted_representation()),
                    arg_str)
      raise
    result_value, _ = value_serialization.deserialize_value(
        result_pb, comp.type_signature.result)
    return type_conversions.type_to_py_container(result_value,
                                                 comp.type_signature.result)
