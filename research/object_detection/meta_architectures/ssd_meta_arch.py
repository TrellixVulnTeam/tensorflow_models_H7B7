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
"""SSD Meta-architecture definition.

General tensorflow implementation of convolutional Multibox/SSD detection
models.
"""
from abc import abstractmethod

import re
import tensorflow as tf

from object_detection.core import box_list
from object_detection.core import box_predictor as bpredictor
from object_detection.core import class_predictor as cpredictor
from object_detection.core import model
from object_detection.core import standard_fields as fields
from object_detection.core import target_assigner
from object_detection.utils import shape_utils
from object_detection.utils import visualization_utils
from object_detection.core import preprocessor

slim = tf.contrib.slim


class SSDFeatureExtractor(object):
  """SSD Feature Extractor definition."""

  def __init__(self,
               is_training,
               depth_multiplier,
               min_depth,
               pad_to_multiple,
               conv_hyperparams,
               batch_norm_trainable=True,
               reuse_weights=None):
    """Constructor.

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
      reuse_weights: whether to reuse variables. Default is None.
    """
    self._is_training = is_training
    self._depth_multiplier = depth_multiplier
    self._min_depth = min_depth
    self._pad_to_multiple = pad_to_multiple
    self._conv_hyperparams = conv_hyperparams
    self._batch_norm_trainable = batch_norm_trainable
    self._reuse_weights = reuse_weights

  @abstractmethod
  def preprocess(self, resized_inputs):
    """Preprocesses images for feature extraction (minus image resizing).

    Args:
      resized_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.

    Returns:
      preprocessed_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.
    """
    pass

  @abstractmethod
  def extract_features(self, preprocessed_inputs):
    """Extracts features from preprocessed inputs.

    This function is responsible for extracting feature maps from preprocessed
    images.

    Args:
      preprocessed_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.

    Returns:
      feature_maps: a list of tensors where the ith tensor has shape
        [batch, height_i, width_i, depth_i]
    """
    pass


class SSDMetaArch(model.DetectionModel):
  """SSD Meta-architecture definition."""

  def __init__(self,
               is_training,
               anchor_generator,
               box_predictor,
               class_predictor,
               box_coder,
               feature_extractor,
               matcher,
               region_similarity_calculator,
               image_resizer_fn,
               non_max_suppression_fn,
               score_conversion_fn,
               classification_loss,
               localization_loss,
               classification_in_image_level_loss,
               classification_loss_weight,
               localization_loss_weight,
               classification_in_image_level_loss_weight,
               normalize_loss_by_num_matches,
               hard_example_miner,
               add_summaries=True):
    """SSDMetaArch Constructor.

    TODO: group NMS parameters + score converter into a class and loss
    parameters into a class and write config protos for postprocessing
    and losses.

    Args:
      is_training: A boolean indicating whether the training version of the
        computation graph should be constructed.
      anchor_generator: an anchor_generator.AnchorGenerator object.
      box_predictor: a box_predictor.BoxPredictor object.
      box_coder: a box_coder.BoxCoder object.
      feature_extractor: a SSDFeatureExtractor object.
      matcher: a matcher.Matcher object.
      region_similarity_calculator: a
        region_similarity_calculator.RegionSimilarityCalculator object.
      image_resizer_fn: a callable for image resizing.  This callable always
        takes a rank-3 image tensor (corresponding to a single image) and
        returns a rank-3 image tensor, possibly with new spatial dimensions.
        See builders/image_resizer_builder.py.
      non_max_suppression_fn: batch_multiclass_non_max_suppression
        callable that takes `boxes`, `scores` and optional `clip_window`
        inputs (with all other inputs already set) and returns a dictionary
        hold tensors with keys: `detection_boxes`, `detection_scores`,
        `detection_classes` and `num_detections`. See `post_processing.
        batch_multiclass_non_max_suppression` for the type and shape of these
        tensors.
      score_conversion_fn: callable elementwise nonlinearity (that takes tensors
        as inputs and returns tensors).  This is usually used to convert logits
        to probabilities.
      classification_loss: an object_detection.core.losses.Loss object.
      localization_loss: a object_detection.core.losses.Loss object.
      classification_loss_weight: float
      localization_loss_weight: float
      normalize_loss_by_num_matches: boolean
      hard_example_miner: a losses.HardExampleMiner object (can be None)
      add_summaries: boolean (default: True) controlling whether summary ops
        should be added to tensorflow graph.
    """
    super(SSDMetaArch, self).__init__(num_classes=box_predictor.num_classes)

    self._is_training = is_training

    # Needed for fine-tuning from classification checkpoints whose
    # variables do not have the feature extractor scope.
    self._extract_features_scope = 'FeatureExtractor'

    self._anchor_generator = anchor_generator
    self._box_predictor = box_predictor
    self._class_predictor = class_predictor

    self._box_coder = box_coder
    self._feature_extractor = feature_extractor
    self._matcher = matcher
    self._region_similarity_calculator = region_similarity_calculator

    # TODO: handle agnostic mode and positive/negative class weights
    unmatched_cls_target = None
    unmatched_cls_target = tf.constant([1] + self.num_classes * [0], tf.float32)

    self._target_assigner = target_assigner.TargetAssigner(
        self._region_similarity_calculator,
        self._matcher,
        self._box_coder,
        positive_class_weight=1.0,
        negative_class_weight=1.0,
        unmatched_cls_target=unmatched_cls_target)

    self._classification_loss = classification_loss
    self._localization_loss = localization_loss
    self._classification_in_image_level_loss = classification_in_image_level_loss
    self._classification_loss_weight = classification_loss_weight
    self._localization_loss_weight = localization_loss_weight
    self._classification_in_image_level_loss_weight = classification_in_image_level_loss_weight
    self._normalize_loss_by_num_matches = normalize_loss_by_num_matches
    self._hard_example_miner = hard_example_miner

    self._image_resizer_fn = image_resizer_fn
    self._non_max_suppression_fn = non_max_suppression_fn
    self._score_conversion_fn = score_conversion_fn

    self._anchors = None
    self._add_summaries = add_summaries

  @property
  def anchors(self):
    if not self._anchors:
      raise RuntimeError('anchors have not been constructed yet!')
    if not isinstance(self._anchors, box_list.BoxList):
      raise RuntimeError('anchors should be a BoxList object, but is not.')
    return self._anchors

  def preprocess(self, inputs, audio_flag=False):
    """Feature-extractor specific preprocessing.

    See base class.

    Args:
      inputs: a [batch, height_in, width_in, channels] float tensor representing
        a batch of images with values between 0 and 255.0.

    Returns:
      preprocessed_inputs: a [batch, height_out, width_out, channels] float
        tensor representing a batch of images.
    Raises:
      ValueError: if inputs tensor does not have type tf.float32
    """
    if inputs.dtype is not tf.float32:
      raise ValueError('`preprocess` expects a tf.float32 tensor')
    with tf.name_scope('Preprocessor'):
      # TODO: revisit whether to always use batch size as the number of parallel
      # iterations vs allow for dynamic batching.

      if (audio_flag == False):
        resized_inputs = tf.map_fn(self._image_resizer_fn,
                                   elems=inputs,
                                   dtype=tf.float32)
        return self._feature_extractor.preprocess(resized_inputs, normalized=False)
      else:
        print("audio shape in preprocess", inputs.get_shape())
        # no resize
        # TO DO: hard coding for just coverting
        # dynamic shape of tensor to the static shape
        # need to be modified later
        resized_inputs = preprocessor.resize_image(inputs,
                                                   new_height=20,
                                                   new_width=9)

        return self._feature_extractor.preprocess(resized_inputs, normalized=False)


  def predict(self, preprocessed_inputs, preprocessed_second_inputs):
    """Predicts unpostprocessed tensors from input tensor.

    This function takes an input batch of images and runs it through the forward
    pass of the network to yield unpostprocessesed predictions.

    A side effect of calling the predict method is that self._anchors is
    populated with a box_list.BoxList of anchors.  These anchors must be
    constructed before the postprocess or loss functions can be called.

    Args:
      preprocessed_inputs: a [batch, height, width, channels] image tensor.

    Returns:
      prediction_dict: a dictionary holding "raw" prediction tensors:
        1) box_encodings: 4-D float tensor of shape [batch_size, num_anchors,
          box_code_dimension] containing predicted boxes.
        2) class_predictions_with_background: 3-D float tensor of shape
          [batch_size, num_anchors, num_classes+1] containing class predictions
          (logits) for each of the anchors.  Note that this tensor *includes*
          background class predictions (at class index 0).
        3) feature_maps: a list of tensors where the ith tensor has shape
          [batch, height_i, width_i, depth_i].
        4) anchors: 2-D float tensor of shape [num_anchors, 4] containing
          the generated anchors in normalized coordinates.
    """
    with tf.variable_scope(None, self._extract_features_scope,
                           [preprocessed_inputs]):
      #feature_maps = self._feature_extractor.extract_features(
      feature_maps, audio_features = self._feature_extractor.extract_features(
          preprocessed_inputs, preprocessed_second_inputs)
    feature_map_spatial_dims = self._get_feature_map_spatial_dims(feature_maps)
    image_shape = tf.shape(preprocessed_inputs)
    self._anchors = self._anchor_generator.generate(
        feature_map_spatial_dims,
        im_height=image_shape[1],
        im_width=image_shape[2])
    #(box_encodings, class_predictions_with_background
    #) = self._add_box_predictions_to_feature_maps(feature_maps)

    (box_encodings, class_predictions_with_background
    ) = self._add_box_predictions_to_feature_maps(feature_maps, audio_features)

    #class_predictions_in_image_level = self._add_class_predictions_to_feature_maps(feature_maps)
    class_predictions_in_image_level = self._add_class_predictions_to_feature_maps(
                                       feature_maps, audio_features)

    predictions_dict = {
        'box_encodings': box_encodings,
        'class_predictions_with_background': class_predictions_with_background,
        'class_predictions_in_image_level': class_predictions_in_image_level,
        'feature_maps': feature_maps,
        'anchors': self._anchors.get()
    }
    return predictions_dict

  #def _add_class_predictions_to_feature_maps(self, feature_maps):
  def _add_class_predictions_to_feature_maps(self, feature_maps, audio_features):
    """Add class predictors (image-level) if using multi-task-labels
       Args:
        feature_maps: multi-resolution feature maps
       Returns:
        cls_predictions_in_image_level: [batch_size, num_classes]
       RuntimeError: if the number of feature maps extracted via the
            extract_features method does not match the length of the
            num_anchors_per_locations list that was passed to the constructor.
    """

    #combined_shape = shape_utils.combined_static_and_dynamic_shape(feature_maps[0])
    #batch_size = combined_shape[0]
    #print("batch_size", batch_size)

    # pickt the one of feature map
    # currently use [batch, 8, 8, 2048]
    #feature_map = feature_maps[2];
    # from multi-resolution feature maps
    # currently use [batch, 4, 4, 512]
    #feature_map = feature_maps[3];
    # batch x 2 x 2 x 256
    #feature_map = feature_maps[4];
    # batch x 1 x 1 x 128
    feature_map = feature_maps[5];

    class_predictor_scope = 'ClassPredictor'

    class_predictions = self._class_predictor.predict(feature_map,
                                                    audio_features,
                                                    class_predictor_scope)
    cls_predictions_in_image_level = class_predictions[
        cpredictor.IMAGE_LEVEL_CLASS_PREDICTIONS]

    """
    class_predictions = self._class_predictor.predict(feature_map,
                                                    class_predictor_scope)
    """


    """
    cls_predictions_in_image_level_list = []
    for idx, feature_map in enumerate(feature_maps):
      class_predictor_scope = 'ClassPredictor_{}'.format(idx)
      #print ('idx = %d' % (idx))
      class_predictions = self._class_predictor.predict(feature_map,
                                                    class_predictor_scope)
      cls_predictions_in_image_level = class_predictions[
          cpredictor.IMAGE_LEVEL_CLASS_PREDICTIONS]

      cls_predictions_in_image_level_list.append(
          cls_predictions_in_image_level)
    """

    #class_predictions_in_image_level = tf.concat(cls_predictions_in_image_level_list, 0)
    #class_predictions_in_image_level = tf.accumulate_n(cls_predictions_in_image_level_list)
    #class_predictions_in_image_level = tf.div(class_predictions_in_image_level,
    #                                          len(cls_predictions_in_image_level_list))
    #class_predictions_in_image_level = tf.reduce_mean(class_predictions_in_image_level, 0, keepdims=True)

    #print("class_predictions_in_image_level", class_predictions_in_image_level.get_shape())
    
    return cls_predictions_in_image_level
    #return class_predictions_in_image_level

  #def _add_box_predictions_to_feature_maps(self, feature_maps):
  def _add_box_predictions_to_feature_maps(self, feature_maps, audio_features):
    """Adds box predictors to each feature map and returns concatenated results.

    Args:
      feature_maps: a list of tensors where the ith tensor has shape
        [batch, height_i, width_i, depth_i]

    Returns:
      box_encodings: 3-D float tensor of shape [batch_size, num_anchors,
          box_code_dimension] containing predicted boxes.
      class_predictions_with_background: 3-D float tensor of shape
          [batch_size, num_anchors, num_classes+1] containing class predictions
          (logits) for each of the anchors.  Note that this tensor *includes*
          background class predictions (at class index 0).

    Raises:
      RuntimeError: if the number of feature maps extracted via the
        extract_features method does not match the length of the
        num_anchors_per_locations list that was passed to the constructor.
      RuntimeError: if box_encodings from the box_predictor does not have
        shape of the form  [batch_size, num_anchors, 1, code_size].
    """
    num_anchors_per_location_list = (
        self._anchor_generator.num_anchors_per_location())
    if len(feature_maps) != len(num_anchors_per_location_list):
      raise RuntimeError('the number of feature maps must match the '
                         'length of self.anchors.NumAnchorsPerLocation().')

    box_encodings_list = []
    cls_predictions_with_background_list = []
    for idx, (feature_map, num_anchors_per_location
             ) in enumerate(zip(feature_maps, num_anchors_per_location_list)):
      box_predictor_scope = 'BoxPredictor_{}'.format(idx)
      """
      box_predictions = self._box_predictor.predict(feature_map,
                                                    num_anchors_per_location,
                                                    box_predictor_scope)
      """
      box_predictions = self._box_predictor.predict(feature_map,
                                                    audio_features,
                                                    num_anchors_per_location,
                                                    box_predictor_scope)

      box_encodings = box_predictions[bpredictor.BOX_ENCODINGS]
      cls_predictions_with_background = box_predictions[
          bpredictor.CLASS_PREDICTIONS_WITH_BACKGROUND]

      box_encodings_shape = box_encodings.get_shape().as_list()
      if len(box_encodings_shape) != 4 or box_encodings_shape[2] != 1:
        raise RuntimeError('box_encodings from the box_predictor must be of '
                           'shape `[batch_size, num_anchors, 1, code_size]`; '
                           'actual shape', box_encodings_shape)
      box_encodings = tf.squeeze(box_encodings, axis=2)
      box_encodings_list.append(box_encodings)
      cls_predictions_with_background_list.append(
          cls_predictions_with_background)

    num_predictions = sum(
        [tf.shape(box_encodings)[1] for box_encodings in box_encodings_list])
    num_anchors = self.anchors.num_boxes()
    anchors_assert = tf.assert_equal(num_anchors, num_predictions, [
        'Mismatch: number of anchors vs number of predictions', num_anchors,
        num_predictions
    ])
    with tf.control_dependencies([anchors_assert]):
      box_encodings = tf.concat(box_encodings_list, 1)
      class_predictions_with_background = tf.concat(
          cls_predictions_with_background_list, 1)
    return box_encodings, class_predictions_with_background

  def _get_feature_map_spatial_dims(self, feature_maps):
    """Return list of spatial dimensions for each feature map in a list.

    Args:
      feature_maps: a list of tensors where the ith tensor has shape
          [batch, height_i, width_i, depth_i].

    Returns:
      a list of pairs (height, width) for each feature map in feature_maps
    """
    feature_map_shapes = [
        shape_utils.combined_static_and_dynamic_shape(
            feature_map) for feature_map in feature_maps
    ]
    return [(shape[1], shape[2]) for shape in feature_map_shapes]

  def postprocess(self, prediction_dict):
    """Converts prediction tensors to final detections.

    This function converts raw predictions tensors to final detection results by
    slicing off the background class, decoding box predictions and applying
    non max suppression and clipping to the image window.

    See base class for output format conventions.  Note also that by default,
    scores are to be interpreted as logits, but if a score_conversion_fn is
    used, then scores are remapped (and may thus have a different
    interpretation).

    Args:
      prediction_dict: a dictionary holding prediction tensors with
        1) box_encodings: 3-D float tensor of shape [batch_size, num_anchors,
          box_code_dimension] containing predicted boxes.
        2) class_predictions_with_background: 3-D float tensor of shape
          [batch_size, num_anchors, num_classes+1] containing class predictions
          (logits) for each of the anchors.  Note that this tensor *includes*
          background class predictions.

    Returns:
      detections: a dictionary containing the following fields
        detection_boxes: [batch, max_detections, 4]
        detection_scores: [batch, max_detections]
        detection_classes: [batch, max_detections]
        detection_keypoints: [batch, max_detections, num_keypoints, 2] (if
          encoded in the prediction_dict 'box_encodings')
        num_detections: [batch]
    Raises:
      ValueError: if prediction_dict does not contain `box_encodings` or
        `class_predictions_with_background` fields.
    """
    if ('box_encodings' not in prediction_dict or
        'class_predictions_in_image_level' not in prediction_dict or
        'class_predictions_with_background' not in prediction_dict):
      raise ValueError('prediction_dict does not contain expected entries.')
    with tf.name_scope('Postprocessor'):
      box_encodings = prediction_dict['box_encodings']
      class_predictions = prediction_dict['class_predictions_with_background']
      class_predictions_in_image_level = prediction_dict['class_predictions_in_image_level']
      detection_boxes, detection_keypoints = self._batch_decode(box_encodings)
      detection_boxes = tf.expand_dims(detection_boxes, axis=2)

      class_predictions_without_background = tf.slice(class_predictions,
                                                      [0, 0, 1],
                                                      [-1, -1, -1])
      detection_scores = self._score_conversion_fn(
          class_predictions_without_background)
      """
      class_predictions_in_image_level = tf.Print(class_predictions_in_image_level,
                                                 [class_predictions_in_image_level],
                                                  message="class_predictions_in_image_level: ")
      """

      #class_scores_in_image_level = slim.fully_connected(class_predictions_in_image_level, self._class_predictor.num_classes)
      class_scores_in_image_level = self._score_conversion_fn(class_predictions_in_image_level)
      #print ("class_scores_in_image_level_shape", class_scores_in_image_level.get_shape())

      nmsed_scores_in_image_level = tf.reduce_max(class_scores_in_image_level, axis=1)
      #print ("nmsed_scores_in_image_level", nmsed_scores_in_image_level.get_shape())

      nmsed_classes_in_image_level = tf.argmax(class_scores_in_image_level, axis=1)
      #print ("nmsed_classesin_image_level", nmsed_classes_in_image_level.get_shape())

      clip_window = tf.constant([0, 0, 1, 1], tf.float32)
      additional_fields = None
      if detection_keypoints is not None:
        additional_fields = {
            fields.BoxListFields.keypoints: detection_keypoints}
      (nmsed_boxes, nmsed_scores, nmsed_classes, _, nmsed_additional_fields,
       num_detections) = self._non_max_suppression_fn(
           detection_boxes,
           detection_scores,
           clip_window=clip_window,
           additional_fields=additional_fields)
      detection_dict = {'detection_boxes': nmsed_boxes,
                        'detection_scores': nmsed_scores,
                        'detection_classes': nmsed_classes,
                        'num_detections': tf.to_float(num_detections),
                        'detection_scores_in_image_level': nmsed_scores_in_image_level,
                        'detection_classes_in_image_level': nmsed_classes_in_image_level
                       }
      if (nmsed_additional_fields is not None and
          fields.BoxListFields.keypoints in nmsed_additional_fields):
        detection_dict['detection_keypoints'] = nmsed_additional_fields[
            fields.BoxListFields.keypoints]
      return detection_dict

  def loss(self, prediction_dict, scope=None):
    """Compute scalar loss tensors with respect to provided groundtruth.

    Calling this function requires that groundtruth tensors have been
    provided via the provide_groundtruth function.

    Args:
      prediction_dict: a dictionary holding prediction tensors with
        1) box_encodings: 3-D float tensor of shape [batch_size, num_anchors,
          box_code_dimension] containing predicted boxes.
        2) class_predictions_with_background: 3-D float tensor of shape
          [batch_size, num_anchors, num_classes+1] containing class predictions
          (logits) for each of the anchors. Note that this tensor *includes*
          background class predictions.
      scope: Optional scope name.

    Returns:
      a dictionary mapping loss keys (`localization_loss` and
        `classification_loss`) to scalar tensors representing corresponding loss
        values.
    """
    with tf.name_scope(scope, 'Loss', prediction_dict.values()):
      keypoints = None
      if self.groundtruth_has_field(fields.BoxListFields.keypoints):
        keypoints = self.groundtruth_lists(fields.BoxListFields.keypoints)
      (batch_cls_targets, batch_cls_weights, batch_reg_targets,
       batch_reg_weights, batch_cls_targets_in_image_level, match_list) = self._assign_targets(
           self.groundtruth_lists(fields.BoxListFields.boxes),
           self.groundtruth_lists(fields.BoxListFields.classes),
           self.groundtruth_lists(fields.BoxListFields.image_level_classes),
           keypoints)
      if self._add_summaries:
        self._summarize_input(
            self.groundtruth_lists(fields.BoxListFields.boxes), match_list)
      num_matches = tf.stack(
          [match.num_matched_columns() for match in match_list])
      location_losses = self._localization_loss(
          prediction_dict['box_encodings'],
          batch_reg_targets,
          ignore_nan_targets=True,
          weights=batch_reg_weights)
      cls_losses = self._classification_loss(
          prediction_dict['class_predictions_with_background'],
          batch_cls_targets,
          weights=batch_cls_weights)
      cls_losses_in_image_level = self._classification_in_image_level_loss(
          prediction_dict['class_predictions_in_image_level'],
          batch_cls_targets_in_image_level)

      if self._hard_example_miner:
        (localization_loss, classification_loss) = self._apply_hard_mining(
            location_losses, cls_losses, prediction_dict, match_list)
        if self._add_summaries:
          self._hard_example_miner.summarize()
      else:
        if self._add_summaries:
          class_ids = tf.argmax(batch_cls_targets, axis=2)
          flattened_class_ids = tf.reshape(class_ids, [-1])
          flattened_classification_losses = tf.reshape(cls_losses, [-1])
          self._summarize_anchor_classification_loss(
              flattened_class_ids, flattened_classification_losses)
        localization_loss = tf.reduce_sum(location_losses)
        classification_loss = tf.reduce_sum(cls_losses)
      
      classification_loss_in_image_level = tf.reduce_sum(cls_losses_in_image_level)

      # Optionally normalize by number of positive matches
      normalizer = tf.constant(1.0, dtype=tf.float32)
      if self._normalize_loss_by_num_matches:
        normalizer = tf.maximum(tf.to_float(tf.reduce_sum(num_matches)), 1.0)

      with tf.name_scope('localization_loss'):
        localization_loss = ((self._localization_loss_weight / normalizer) *
                             localization_loss)
      with tf.name_scope('classification_loss'):
        classification_loss = ((self._classification_loss_weight / normalizer) *
                               classification_loss)

      with tf.name_scope('classification_loss_in_image_level'):
        classification_loss_in_image_level = (self._classification_in_image_level_loss_weight *
                               classification_loss_in_image_level)
      loss_dict = {
          'localization_loss': localization_loss,
          'classification_loss': classification_loss,
          'classification_loss_in_image_level': classification_loss_in_image_level
      }
    return loss_dict

  def _summarize_anchor_classification_loss(self, class_ids, cls_losses):
    positive_indices = tf.where(tf.greater(class_ids, 0))
    positive_anchor_cls_loss = tf.squeeze(
        tf.gather(cls_losses, positive_indices), axis=1)
    visualization_utils.add_cdf_image_summary(positive_anchor_cls_loss,
                                              'PositiveAnchorLossCDF')
    negative_indices = tf.where(tf.equal(class_ids, 0))
    negative_anchor_cls_loss = tf.squeeze(
        tf.gather(cls_losses, negative_indices), axis=1)
    visualization_utils.add_cdf_image_summary(negative_anchor_cls_loss,
                                              'NegativeAnchorLossCDF')

  def _assign_targets(self, groundtruth_boxes_list, groundtruth_classes_list,
                      groundtruth_classes_in_image_level_list, groundtruth_keypoints_list=None):
    """Assign groundtruth targets.

    Adds a background class to each one-hot encoding of groundtruth classes
    and uses target assigner to obtain regression and classification targets.

    Args:
      groundtruth_boxes_list: a list of 2-D tensors of shape [num_boxes, 4]
        containing coordinates of the groundtruth boxes.
          Groundtruth boxes are provided in [y_min, x_min, y_max, x_max]
          format and assumed to be normalized and clipped
          relative to the image window with y_min <= y_max and x_min <= x_max.
      groundtruth_classes_list: a list of 2-D one-hot (or k-hot) tensors of
        shape [num_boxes, num_classes] containing the class targets with the 0th
        index assumed to map to the first non-background class.
      groundtruth_classes_image_level_list: a list of 1-D one-hot (or k-hot) tensors of
        shape [num_classes] containing the class targets
      groundtruth_keypoints_list: (optional) a list of 3-D tensors of shape
        [num_boxes, num_keypoints, 2]

    Returns:
      batch_cls_targets: a tensor with shape [batch_size, num_anchors,
        num_classes],
      batch_cls_weights: a tensor with shape [batch_size, num_anchors],
      batch_reg_targets: a tensor with shape [batch_size, num_anchors,
        box_code_dimension]
      batch_reg_weights: a tensor with shape [batch_size, num_anchors],
      match_list: a list of matcher.Match objects encoding the match between
        anchors and groundtruth boxes for each image of the batch,
        with rows of the Match objects corresponding to groundtruth boxes
        and columns corresponding to anchors.
    """
    groundtruth_boxlists = [
        box_list.BoxList(boxes) for boxes in groundtruth_boxes_list
    ]
    groundtruth_classes_with_background_list = [
        tf.pad(one_hot_encoding, [[0, 0], [1, 0]], mode='CONSTANT')
        for one_hot_encoding in groundtruth_classes_list
    ]

    groundtruth_classes_in_image_level_list = [
        tf.pad(one_hot_encoding, [[1, 0]], mode='CONSTANT')
        for one_hot_encoding in groundtruth_classes_in_image_level_list
    ]

    if groundtruth_keypoints_list is not None:
      for boxlist, keypoints in zip(
          groundtruth_boxlists, groundtruth_keypoints_list):
        boxlist.add_field(fields.BoxListFields.keypoints, keypoints)
    return target_assigner.batch_assign_targets(
        self._target_assigner, self.anchors, groundtruth_boxlists,
        groundtruth_classes_with_background_list,
        groundtruth_classes_in_image_level_list)

  def _summarize_input(self, groundtruth_boxes_list, match_list):
    """Creates tensorflow summaries for the input boxes and anchors.

    This function creates four summaries corresponding to the average
    number (over images in a batch) of (1) groundtruth boxes, (2) anchors
    marked as positive, (3) anchors marked as negative, and (4) anchors marked
    as ignored.

    Args:
      groundtruth_boxes_list: a list of 2-D tensors of shape [num_boxes, 4]
        containing corners of the groundtruth boxes.
      match_list: a list of matcher.Match objects encoding the match between
        anchors and groundtruth boxes for each image of the batch,
        with rows of the Match objects corresponding to groundtruth boxes
        and columns corresponding to anchors.
    """
    num_boxes_per_image = tf.stack(
        [tf.shape(x)[0] for x in groundtruth_boxes_list])
    pos_anchors_per_image = tf.stack(
        [match.num_matched_columns() for match in match_list])
    neg_anchors_per_image = tf.stack(
        [match.num_unmatched_columns() for match in match_list])
    ignored_anchors_per_image = tf.stack(
        [match.num_ignored_columns() for match in match_list])
    tf.summary.scalar('Input/AvgNumGroundtruthBoxesPerImage',
                      tf.reduce_mean(tf.to_float(num_boxes_per_image)))
    tf.summary.scalar('Input/AvgNumPositiveAnchorsPerImage',
                      tf.reduce_mean(tf.to_float(pos_anchors_per_image)))
    tf.summary.scalar('Input/AvgNumNegativeAnchorsPerImage',
                      tf.reduce_mean(tf.to_float(neg_anchors_per_image)))
    tf.summary.scalar('Input/AvgNumIgnoredAnchorsPerImage',
                      tf.reduce_mean(tf.to_float(ignored_anchors_per_image)))

  def _apply_hard_mining(self, location_losses, cls_losses, prediction_dict,
                         match_list):
    """Applies hard mining to anchorwise losses.

    Args:
      location_losses: Float tensor of shape [batch_size, num_anchors]
        representing anchorwise location losses.
      cls_losses: Float tensor of shape [batch_size, num_anchors]
        representing anchorwise classification losses.
      prediction_dict: p a dictionary holding prediction tensors with
        1) box_encodings: 3-D float tensor of shape [batch_size, num_anchors,
          box_code_dimension] containing predicted boxes.
        2) class_predictions_with_background: 3-D float tensor of shape
          [batch_size, num_anchors, num_classes+1] containing class predictions
          (logits) for each of the anchors.  Note that this tensor *includes*
          background class predictions.
      match_list: a list of matcher.Match objects encoding the match between
        anchors and groundtruth boxes for each image of the batch,
        with rows of the Match objects corresponding to groundtruth boxes
        and columns corresponding to anchors.

    Returns:
      mined_location_loss: a float scalar with sum of localization losses from
        selected hard examples.
      mined_cls_loss: a float scalar with sum of classification losses from
        selected hard examples.
    """
    class_predictions = tf.slice(
        prediction_dict['class_predictions_with_background'], [0, 0,
                                                               1], [-1, -1, -1])

    decoded_boxes, _ = self._batch_decode(prediction_dict['box_encodings'])
    decoded_box_tensors_list = tf.unstack(decoded_boxes)
    class_prediction_list = tf.unstack(class_predictions)
    decoded_boxlist_list = []
    for box_location, box_score in zip(decoded_box_tensors_list,
                                       class_prediction_list):
      decoded_boxlist = box_list.BoxList(box_location)
      decoded_boxlist.add_field('scores', box_score)
      decoded_boxlist_list.append(decoded_boxlist)
    return self._hard_example_miner(
        location_losses=location_losses,
        cls_losses=cls_losses,
        decoded_boxlist_list=decoded_boxlist_list,
        match_list=match_list)

  def _batch_decode(self, box_encodings):
    """Decodes a batch of box encodings with respect to the anchors.

    Args:
      box_encodings: A float32 tensor of shape
        [batch_size, num_anchors, box_code_size] containing box encodings.

    Returns:
      decoded_boxes: A float32 tensor of shape
        [batch_size, num_anchors, 4] containing the decoded boxes.
      decoded_keypoints: A float32 tensor of shape
        [batch_size, num_anchors, num_keypoints, 2] containing the decoded
        keypoints if present in the input `box_encodings`, None otherwise.
    """
    combined_shape = shape_utils.combined_static_and_dynamic_shape(
        box_encodings)
    batch_size = combined_shape[0]
    tiled_anchor_boxes = tf.tile(
        tf.expand_dims(self.anchors.get(), 0), [batch_size, 1, 1])
    tiled_anchors_boxlist = box_list.BoxList(
        tf.reshape(tiled_anchor_boxes, [-1, 4]))
    decoded_boxes = self._box_coder.decode(
        tf.reshape(box_encodings, [-1, self._box_coder.code_size]),
        tiled_anchors_boxlist)
    decoded_keypoints = None
    if decoded_boxes.has_field(fields.BoxListFields.keypoints):
      decoded_keypoints = decoded_boxes.get_field(
          fields.BoxListFields.keypoints)
      num_keypoints = decoded_keypoints.get_shape()[1]
      decoded_keypoints = tf.reshape(
          decoded_keypoints,
          tf.stack([combined_shape[0], combined_shape[1], num_keypoints, 2]))
    decoded_boxes = tf.reshape(decoded_boxes.get(), tf.stack(
        [combined_shape[0], combined_shape[1], 4]))
    return decoded_boxes, decoded_keypoints

  def restore_map(self, from_detection_checkpoint=True):
    """Returns a map of variables to load from a foreign checkpoint.

    See parent class for details.

    Args:
      from_detection_checkpoint: whether to restore from a full detection
        checkpoint (with compatible variable names) or to restore from a
        classification checkpoint for initialization prior to training.

    Returns:
      A dict mapping variable names (to load from a checkpoint) to variables in
      the model graph.
    """
    variables_to_restore = {}
    for variable in tf.global_variables():
      if variable.op.name.startswith(self._extract_features_scope):
        var_name = variable.op.name
        if not from_detection_checkpoint:
          var_name = (re.split('^' + self._extract_features_scope + '/',
                               var_name)[-1])
        variables_to_restore[var_name] = variable
    return variables_to_restore
