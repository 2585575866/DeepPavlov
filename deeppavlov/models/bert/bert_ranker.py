# Copyright 2017 Neural Networks and Deep Learning lab, MIPT
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

import tensorflow as tf
from deeppavlov.core.common.registry import register
from deeppavlov.core.models.lr_scheduled_tf_model import LRScheduledTFModel
from deeppavlov.core.commands.utils import expand_path
from logging import getLogger
import numpy as np

from bert_dp.modeling import BertConfig, BertModel

logger = getLogger(__name__)


@register('bert_ranker')
class BertRankerModel(LRScheduledTFModel):
    # TODO: docs
    # TODO: add head-only pre-training
    def __init__(self, bert_config_file, n_classes, keep_prob,
                 batch_size, num_ranking_samples,
                 num_resp = 1,
                 one_hot_labels=False,
                 attention_probs_keep_prob=None, hidden_keep_prob=None,
                 pretrained_bert=None,
                 resps=None, resp_vecs=None, resp_features=None, resp_eval=True,
                 min_learning_rate=1e-06, **kwargs) -> None:
        super().__init__(**kwargs)

        self.num_resp = num_resp
        self.batch_size = batch_size
        self.num_ranking_samples = num_ranking_samples
        self.resp_eval = resp_eval
        self.n_classes = n_classes
        self.min_learning_rate = min_learning_rate
        self.keep_prob = keep_prob
        self.one_hot_labels = one_hot_labels
        self.resps = resps
        self.resp_vecs = resp_vecs
        self.batch_size = batch_size

        self.bert_config = BertConfig.from_json_file(str(expand_path(bert_config_file)))

        if attention_probs_keep_prob is not None:
            self.bert_config.attention_probs_dropout_prob = 1.0 - attention_probs_keep_prob
        if hidden_keep_prob is not None:
            self.bert_config.hidden_dropout_prob = 1.0 - hidden_keep_prob

        self.sess_config = tf.ConfigProto(allow_soft_placement=True)
        self.sess_config.gpu_options.allow_growth = True
        self.sess = tf.Session(config=self.sess_config)

        self._init_graph()

        self._init_optimizer()

        self.sess.run(tf.global_variables_initializer())

        if pretrained_bert is not None:
            pretrained_bert = str(expand_path(pretrained_bert))

        if tf.train.checkpoint_exists(pretrained_bert) \
                and not tf.train.checkpoint_exists(str(self.load_path.resolve())):
            logger.info('[initializing model with Bert from {}]'.format(pretrained_bert))
            # Exclude optimizer and classification variables from saved variables
            var_list = self._get_saveable_variables(
                exclude_scopes=('Optimizer', 'learning_rate', 'momentum', 'classification'))
            saver = tf.train.Saver(var_list)
            saver.restore(self.sess, pretrained_bert)

        if self.load_path is not None:
            self.load()

        if self.resps is not None and self.resp_vecs is None:
            self.resp_features = [resp_features[0][i * self.batch_size: (i + 1) * self.batch_size]
                                  for i in range(len(resp_features[0]) // batch_size + 1)]
            self.resp_vecs = self(self.resp_features)
            np.save(self.save_path / "resp_vecs", self.resp_vecs)

    def _init_graph(self):
        self._init_placeholders()
        with tf.variable_scope("model"):
            self.bert = BertModel(config=self.bert_config,
                                  is_training=self.is_train_ph,
                                  input_ids=self.input_ids_ph,
                                  input_mask=self.input_masks_ph,
                                  token_type_ids=self.token_types_ph,
                                  use_one_hot_embeddings=False,
                                  )

        output_layer_a = self.bert.get_pooled_output()

        with tf.variable_scope("loss"):
            with tf.variable_scope("loss"):
                self.loss = tf.contrib.losses.metric_learning.npairs_loss(self.y_ph, output_layer_a, output_layer_a)
                self.y_probas = output_layer_a

    def _init_placeholders(self):
        self.input_ids_ph = tf.placeholder(shape=(None, None), dtype=tf.int32, name='ids_ph')
        self.input_masks_ph = tf.placeholder(shape=(None, None), dtype=tf.int32, name='masks_ph')
        self.token_types_ph = tf.placeholder(shape=(None, None), dtype=tf.int32, name='token_types_ph')

        if not self.one_hot_labels:
            self.y_ph = tf.placeholder(shape=(None, ), dtype=tf.int32, name='y_ph')
        else:
            self.y_ph = tf.placeholder(shape=(None, self.n_classes), dtype=tf.float32, name='y_ph')

        self.learning_rate_ph = tf.placeholder_with_default(0.0, shape=[], name='learning_rate_ph')
        self.keep_prob_ph = tf.placeholder_with_default(1.0, shape=[], name='keep_prob_ph')
        self.is_train_ph = tf.placeholder_with_default(False, shape=[], name='is_train_ph')

    def _init_optimizer(self):
        # TODO: use AdamWeightDecay optimizer
        with tf.variable_scope('Optimizer'):
            self.global_step = tf.get_variable('global_step', shape=[], dtype=tf.int32,
                                               initializer=tf.constant_initializer(0), trainable=False)
            self.train_op = self.get_train_op(self.loss, learning_rate=self.learning_rate_ph)

    def _build_feed_dict(self, input_ids, input_masks, token_types, y=None):
        feed_dict = {
            self.input_ids_ph: input_ids,
            self.input_masks_ph: input_masks,
            self.token_types_ph: token_types,
        }
        if y is not None:
            feed_dict.update({
                self.y_ph: y,
                self.learning_rate_ph: max(self.get_learning_rate(), self.min_learning_rate),
                self.keep_prob_ph: self.keep_prob,
                self.is_train_ph: True,
            })

        return feed_dict

    def train_on_batch(self, features, y):
        pass

    def __call__(self, features_list):
        pred = []
        for features in features_list:
            input_ids = [f.input_ids for f in features]
            input_masks = [f.input_mask for f in features]
            input_type_ids = [f.input_type_ids for f in features]
            feed_dict = self._build_feed_dict(input_ids, input_masks, input_type_ids)
            p = self.sess.run(self.y_probas, feed_dict=feed_dict)
            if len(p.shape) == 1:
                p = np.expand_dims(p, 0)
            pred.append(p)
        # interact mode
        if len (features_list[0]) == 1 and len(features_list) == 1:
            s = pred[0] @ self.resp_vecs.T
            ids = np.flip(np.argsort(s[0]), axis=0)[:self.num_resp]
            return [self.resps[i] for i in ids]
        # generate vectors for further usage with the database of responses (and contexts)
        elif len(features_list) != self.num_ranking_samples + 1:
            return np.vstack(pred)
        # return scores including scores on the database of responses if self.resp_vecs is set to True
        else:
            c_vecs = list(pred[0])
            scores = []
            for i in range(len(c_vecs)):
                r_vecs = np.vstack([el[i] for el in pred[1:]])
                if self.resp_eval:
                    r_vecs = np.vstack([r_vecs, self.resp_vecs])
                s = c_vecs[i] @ r_vecs.T
                scores.append(s)
            scores = np.vstack(scores)
            return scores


