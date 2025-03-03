# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""SSDFeatureExtractor for InceptionV3 features."""
import tensorflow as tf

from object_detection.meta_architectures import ssd_meta_arch
from object_detection.models import feature_map_generators
from object_detection.utils import ops
from nets import inception_v3
#from nets import inception_v3_audio

slim = tf.contrib.slim


class SSDInceptionV3FeatureExtractorTwoStream(ssd_meta_arch.SSDFeatureExtractor):
  """SSD Feature Extractor using InceptionV3 features."""

  def __init__(self,
               is_training,
               depth_multiplier,
               min_depth,
               pad_to_multiple,
               conv_hyperparams,
               batch_norm_trainable=True,
               reuse_weights=None):
    """InceptionV3 Feature Extractor for SSD Models.

    Args:
      is_training: whether the network is in training mode.
      depth_multiplier: float depth multiplier for feature extractor.
      min_depth: minimum feature extractor depth.
      pad_to_multiple: the nearest multiple to zero pad the input height and
        width dimensions to.
      conv_hyperparams: tf slim arg_scope for conv2d and separable_conv2d ops.
      batch_norm_trainable: Whether to update batch norm parameters during
        training or not. When training with a small batch size
        (e.g. 1), it is desirable to disable batch norm update and use
        pretrained batch norm params.
      reuse_weights: Whether to reuse variables. Default is None.
    """
    super(SSDInceptionV3FeatureExtractorTwoStream, self).__init__(
        is_training, depth_multiplier, min_depth, pad_to_multiple,
        conv_hyperparams, batch_norm_trainable, reuse_weights)

  def preprocess(self, resized_inputs, normalized=False):
    """SSD preprocessing.

    Maps pixel values to the range [-1, 1].

    Args:
      resized_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.
      noremalized: a flag to indicate whether the input data
        is already normalized or not

    Returns:
      preprocessed_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.
    """
    if normalized == False:
      return (2.0 / 255.0) * resized_inputs - 1.0
    else:
      return resized_inputs

  def extract_audio_features(self, inputs):

    # dict for return
    end_points = {}

    with tf.variable_scope('audioNet', [inputs]):
      net = slim.conv2d(inputs, 32, [5, 3], padding="VALID", scope='conv1')
      end_points['conv1'] = net
      net = slim.conv2d(net, 128, [5, 3], padding="VALID", scope='conv2')
      end_points['conv2'] = net
      prev_shape = net.get_shape()
      net = slim.conv2d(net, 2048, [prev_shape[1], prev_shape[2]], padding="VALID", scope='fc3')
      end_points['fc3'] = net

      net = slim.conv2d(net, 256, [1, 1], scope='fc4')
      end_points['fc4'] = net

      net = slim.flatten(net)
      end_points['flat'] = net

      print("audio features after conv1", end_points['conv1'].get_shape())
      print("audio features after conv2", end_points['conv2'].get_shape())

    return end_points

  def extract_features(self, preprocessed_inputs, preprocessed_second_inputs):
    """Extract features from preprocessed inputs.

    Args:
      preprocessed_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.

    Returns:
      feature_maps: a list of tensors where the ith tensor has shape
        [batch, height_i, width_i, depth_i]
    """
    preprocessed_inputs.get_shape().assert_has_rank(4)
    shape_assert = tf.Assert(
        tf.logical_and(tf.greater_equal(tf.shape(preprocessed_inputs)[1], 33),
                       tf.greater_equal(tf.shape(preprocessed_inputs)[2], 33)),
        ['image size must at least be 33 in both height and width.'])

    feature_map_layout = {
        'from_layer': ['Mixed_5d', 'Mixed_6e', 'Mixed_7c', '', '', ''],
        'layer_depth': [-1, -1, -1, 512, 256, 128],
    }

    #print("preprocessed_second_inputs", preprocessed_second_inputs.get_shape())

    with tf.control_dependencies([shape_assert]):
      with slim.arg_scope(self._conv_hyperparams):
        with tf.variable_scope('InceptionV3',
                               reuse=self._reuse_weights) as scope:

          audio_features = self.extract_audio_features(preprocessed_second_inputs)

          _, image_features = inception_v3.inception_v3_base(
              ops.pad_to_multiple(preprocessed_inputs, self._pad_to_multiple),
#              audio_features['fc5'],
#              audio_features['flat'],
              final_endpoint='Mixed_7c',
              min_depth=self._min_depth,
              depth_multiplier=self._depth_multiplier,
              scope=scope)

          #audio_features = self.extract_audio_features(preprocessed_second_inputs)

          feature_maps = feature_map_generators.multi_resolution_feature_maps(
              feature_map_layout=feature_map_layout,
              depth_multiplier=self._depth_multiplier,
              min_depth=self._min_depth,
              insert_1x1_conv=True,
              image_features=image_features)
              #audio_features=audio_features)

    #return feature_maps.values()
    return feature_maps.values(), audio_features['fc4']
