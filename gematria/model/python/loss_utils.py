# Copyright 2022 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Contains types, classes and functions for loss computation in Gematria."""

from collections.abc import Sequence

from gematria.model.python import options
import tensorflow.compat.v1 as tf
import tensorflow_probability as tfp
import tensorflow_ranking as tfr


# Type of keys used when caching the loss tensors generated by a LossComputation
# object.
_LossTensorType = tuple[options.LossType, options.ErrorNormalization]


class LossComputation:
  """Maintains TF ops for computing loss from actual and expected outputs."""

  def __init__(
      self,
      output_values: tf.Tensor,
      expected_outputs: tf.Tensor,
      mask: tf.Tensor,
      dtype: tf.dtypes.DType,
      percentile_ranks: Sequence[int] = (),
  ):
    """Initializes the loss computation.

    Args:
      output_values: The actual outputs of the model; of shape (N, T) where N is
        the number of samples and T is the number of tasks.
      expected_outputs: The expected outputs of the model; of shape (N, T) where
        N is the number of samples and T is the number of tasks.
      mask: The mask for well defined outputs; of shape (N, T) where N is the
        number of samples and T is the number of tasks. The loss includes only
        outputs where the corresponding entry of the mask is True.
      dtype: The TensorFlow DType used by the model.
      percentile_ranks: The percentile ranks used in the error statistics. These
        must be integers between 0 and 100.
    """
    if len(output_values.shape) != 2:
      raise ValueError(
          'output_values must be a 2D tensor. Actual shape:'
          f' {output_values.shape}'
      )
    if not expected_outputs.shape.is_compatible_with(output_values.shape):
      raise ValueError(
          'Expected expected_outputs.shape to be compatible with '
          f'{output_values.shape}. Found {expected_outputs.shape}'
      )

    self._num_tasks = output_values.shape[1] or expected_outputs.shape[1]
    if not mask.shape.is_compatible_with(output_values.shape):
      raise ValueError(
          'Expected mask.shape to be compatible with'
          f' {output_values.shape}. Found {mask.shape}'
      )
    if tf.dtypes.bool != mask.dtype:
      raise ValueError(
          f'Expected mask.dtype to be tf.dtypes.bool. Found {mask.dtype}.'
      )

    self._percentile_ranks = percentile_ranks
    self._dtype = dtype
    self._loss_tensors: dict[_LossTensorType, tf.Tensor] = {}

    # tf.ragged.boolean_mask() does not have an `axis` argument to control which
    # dimension is ragged and in case of 2D tensors it is always the second one.
    # We transpose the data so that the first (non-ragged) dimension goes along
    # tasks, and the second (ragged) dimension goes along the values.
    # All the tensors below have the shape
    self._mask = tf.transpose(mask)
    self._output_values = tf.ragged.boolean_mask(
        tf.transpose(output_values), self._mask
    )
    assert self._output_values.shape.is_compatible_with((self._num_tasks, None))
    self._expected_outputs = tf.ragged.boolean_mask(
        tf.transpose(expected_outputs), self._mask
    )
    assert self._expected_outputs.shape.is_compatible_with(
        (self._num_tasks, None)
    )

    self._delta = self._output_values - self._expected_outputs
    assert self._delta.shape.is_compatible_with((self._num_tasks, None))

    self._squared_errors = tf.square(self._delta)
    assert self._squared_errors.shape.is_compatible_with(
        (self._num_tasks, None)
    )
    self._absolute_errors = tf.abs(self._delta)
    assert self._absolute_errors.shape.is_compatible_with(
        (self._num_tasks, None)
    )
    self._absolute_percentage_errors = (
        self._absolute_errors / self._expected_outputs
    )
    assert self._absolute_percentage_errors.shape.is_compatible_with(
        (self._num_tasks, None)
    )
    self._squared_percentage_error = tf.square(self._absolute_percentage_errors)
    assert self._squared_percentage_error.shape.is_compatible_with(
        (self._num_tasks, None)
    )
    self._absolute_error_percentiles = self._make_percentile_tensor(
        self._absolute_errors
    )
    assert (
        not self._percentile_ranks
        or self._absolute_error_percentiles.shape.is_compatible_with(
            (len(self._percentile_ranks), self._num_tasks)
        )
    )
    self._absolute_percentage_error_percentiles = self._make_percentile_tensor(
        self._absolute_percentage_errors
    )
    assert (
        not self._percentile_ranks
        or self._absolute_percentage_error_percentiles.shape.is_compatible_with(
            (len(self._percentile_ranks), self._num_tasks)
        )
    )
    # The absolute value of expected_outputs. Contains 1.0 in place of values
    # that are smaller than one.
    self._absolute_expected_outputs_or_one = tf.math.maximum(
        self._expected_outputs,
        tf.ones_like(self._expected_outputs, dtype=dtype),
    )
    assert self._absolute_expected_outputs_or_one.shape.is_compatible_with(
        (self._num_tasks, None)
    )

  @property
  def mean_absolute_error(self) -> tf.Tensor:
    """Returns the mean absolute error."""
    return self.loss_tensor(
        options.ErrorNormalization.NONE, options.LossType.MEAN_ABSOLUTE_ERROR
    )

  @property
  def mean_squared_error(self) -> tf.Tensor:
    """Returns the mean squared error."""
    return self.loss_tensor(
        options.ErrorNormalization.NONE, options.LossType.MEAN_SQUARED_ERROR
    )

  @property
  def mean_absolute_percentage_error(self) -> tf.Tensor:
    """Returns the mean absolute percentager error."""
    return self.loss_tensor(
        options.ErrorNormalization.PERCENTAGE_ERROR,
        options.LossType.MEAN_ABSOLUTE_ERROR,
    )

  @property
  def mean_squared_percentage_error(self) -> tf.Tensor:
    """Returns the mean squared percentage error."""
    return self.loss_tensor(
        options.ErrorNormalization.PERCENTAGE_ERROR,
        options.LossType.MEAN_SQUARED_ERROR,
    )

  @property
  def absolute_error_percentiles(self) -> tf.Tensor:
    """Returns the percentiles of the absolute error."""
    return self._absolute_error_percentiles

  @property
  def absolute_percentage_error_percentiles(self) -> tf.Tensor:
    """Returns the percentiles of the absolute percentage error."""
    return self._absolute_percentage_error_percentiles

  def loss_tensor(
      self,
      normalization: options.ErrorNormalization,
      loss_type: options.LossType,
  ) -> tf.Tensor:
    """Returns a loss tensor of the given type.

    Args:
      normalization: Determines whether and how the errors in the loss tensor
        are normalized.
      loss_type: The type of loss.

    Returns:
      A tensor that contains the requested loss. When called multiple times with
      the same arguments, this method will always return the same tensor object.
      The returned tensor is of shape (N, T), where T is the number of tasks.
    """
    tensor = self._loss_tensors.get((loss_type, normalization))
    if tensor is None:
      match loss_type:
        case options.LossType.MEAN_SQUARED_ERROR:
          tensor = tf.reduce_mean(
              self._squared_errors_witn_normalization(normalization), axis=1
          )
        case options.LossType.MEAN_ABSOLUTE_ERROR:
          tensor = tf.reduce_mean(
              self._absolute_errors_with_normalization(normalization), axis=1
          )
        case options.LossType.HUBER:
          absolute_errors = self._absolute_errors_with_normalization(
              normalization
          )
          # The delta parameter from the Huber loss definition.
          huber_delta = tf.constant(1.0, dtype=self._dtype)
          # The expression in the quadratic part of the Huber loss expression.
          # It is squared in the return statement below.
          quadratic = tf.minimum(absolute_errors, huber_delta)
          # The linear part of the Huber loss expression. This is zero when
          # absolute_error <= huber_delta.
          linear = absolute_errors - quadratic
          tensor = tf.reduce_mean(
              0.5 * tf.square(quadratic) + huber_delta * linear, axis=1
          )
        case options.LossType.RANKING_SOFTMAX_LOSS:
          loss_fn = tfr.losses.make_loss_fn(
              tfr.losses.RankingLossKey.SOFTMAX_LOSS
          )
          loss_tensors = []
          # The loss functions in tensorflow_ranking collapse the whole input
          # into a single number. To support multi-task learning, we need to
          # compute the loss for each task separately and then concatenate them.
          for task in range(self._num_tasks):
            loss_tensors.append(
                tf.reshape(
                    loss_fn(
                        # The tensorflow_ranking library expects inputs of shape
                        # (num_batches, num_items_in_batch). In our case, we do
                        # not have multiple ranking requests to batch them, so
                        # we turn each batch into a single ranking request with
                        # all basic blocks in the batch.
                        tf.reshape(self._expected_outputs[task], (1, -1)),
                        tf.reshape(self._output_values[task], (1, -1)),
                        None,
                    ),
                    (1,),
                )
            )
          tensor = tf.concat(loss_tensors, axis=0)
        case _:
          raise ValueError(f'Unexpected loss type: {loss_type}')
      assert tensor.shape.is_compatible_with((
          self._num_tasks,
      )), f'The actual shape is {tensor.shape}'
      self._loss_tensors[loss_type, normalization] = tensor
    return tensor

  def _make_percentile_tensor(self, values: tf.RaggedTensor) -> tf.Tensor:
    """Creates a percentile tensor from 'values' using self.percentile_ranks.

    Args:
      values: A 2D ragged tensor from which the percentiles are collected. The
        percentiles are collected along the axis 0 of `values`.

    Returns:
      Percentiles based on self_percentile_ranks and the values. The returned
      tensor is of shape (N_PERCENTILE_RANKS, T), where T is the number of
      tasks.
    """
    if not self._percentile_ranks:
      return tf.constant([], dtype=self._dtype)

    percentile_tensors = []
    # NOTE(ondrasej): As of Nov 2022, tfp.stats.percentile() is not compatible
    # with ragged tensors, so we need to split the ragged tensor into rows and
    # then stack the individual percentile tensors to the desired output shape.
    for task in range(self._num_tasks):
      task_values = values[task]
      percentile_tensors.append(
          tfp.stats.percentile(task_values, self._percentile_ranks)
      )
      assert percentile_tensors[-1].shape.is_compatible_with((None,))
    return tf.stack(percentile_tensors, axis=1)

  def _squared_errors_witn_normalization(
      self, normalization: options.ErrorNormalization
  ) -> tf.Tensor:
    """Returns the tensor of squared errors."""
    match normalization:
      case options.ErrorNormalization.NONE:
        result = self._squared_errors
      case options.ErrorNormalization.PERCENTAGE_ERROR:
        result = self._squared_percentage_error
      case options.ErrorNormalization.EXPECTED_VALUE_GREATER_THAN_ONE:
        result = tf.square(self._delta / self._absolute_expected_outputs_or_one)
      case _:
        raise NotImplementedError(
            f'Squared errors not implemented yet: {normalization}'
        )
    assert result.shape.is_compatible_with(
        (self._num_tasks, None)
    ), f'Actual shape of the squared errors tensor is {result.shape}'
    return result

  def _absolute_errors_with_normalization(
      self, normalization: options.ErrorNormalization
  ) -> tf.Tensor:
    """Returns the tensor of absolute errors."""
    match normalization:
      case options.ErrorNormalization.NONE:
        return self._absolute_errors
      case options.ErrorNormalization.PERCENTAGE_ERROR:
        return self._absolute_percentage_errors
      case options.ErrorNormalization.EXPECTED_VALUE_GREATER_THAN_ONE:
        return self._absolute_errors / self._absolute_expected_outputs_or_one
      case _:
        raise NotImplementedError(
            'Absolute errors not implemented yet: {normalization}'
        )
