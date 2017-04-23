import numpy as np
import tensorflow as tf
from keras.layers import Dense
from keras.engine.topology import Layer
from keras import backend as K


def _nan2zero(x):
    return tf.where(tf.is_nan(x), tf.zeros_like(x), x)


def _nelem(x):
    nelem = tf.reduce_sum(tf.cast(~tf.is_nan(x), tf.float32))
    return tf.cast(tf.where(tf.equal(nelem, 0.), 1., nelem), x.dtype)


def _reduce_mean(x):
    nelem = _nelem(x)
    x = _nan2zero(x)
    return tf.divide(tf.reduce_sum(x), nelem)


def mse_loss(y_true, y_pred):
    ret = tf.square(y_pred - y_true)

    return _reduce_mean(ret)


# In the implementations, I try to keep the function signature
# similar to those of Keras objective functions so that
# later on we can use them in Keras smoothly:
# https://github.com/fchollet/keras/blob/master/keras/objectives.py#L7
def poisson_loss(y_true, y_pred):
    y_pred = tf.cast(y_pred, tf.float32)
    y_true = tf.cast(y_true, tf.float32)

    # we can use the Possion PMF from TensorFlow as well
    # dist = tf.contrib.distributions
    # return -tf.reduce_mean(dist.Poisson(y_pred).log_pmf(y_true))

    nelem = _nelem(y_true)
    y_true = _nan2zero(y_true)

    # last term can be avoided since it doesn't depend on y_pred
    # however keeping it gives a nice lower bound to zero
    ret = y_pred - y_true*tf.log(y_pred+1e-10) + tf.lgamma(y_true+1.0)

    return tf.divide(tf.reduce_sum(ret), nelem)


# We need a class (or closure) here,
# because it's not possible to
# pass extra arguments to Keras loss functions
# See https://github.com/fchollet/keras/issues/2121

# dispersion (theta) parameter is a scalar by default.
# scale_factor scales the nbinom mean before the
# calculation of the loss to balance the
# learning rates of theta and network weights
class NB(object):
    def __init__(self, theta=None, masking=False, scope='nbinom_loss/',
                 scale_factor=1.0, debug=False):

        # for numerical stability
        self.eps = 1e-10
        self.scale_factor = scale_factor
        self.debug = debug
        self.scope = scope
        self.masking = masking
        self.theta = theta

    def loss(self, y_true, y_pred, reduce=True):
        scale_factor = self.scale_factor
        eps = self.eps

        with tf.name_scope(self.scope):
            y_true = tf.cast(y_true, tf.float32)
            y_pred = tf.cast(y_pred, tf.float32) * scale_factor

            if self.masking:
                nelem = _nelem(y_true)
                y_true = _nan2zero(y_true)

            theta = self.theta
            t1 = -tf.lgamma(y_true+theta+eps)
            t2 = tf.lgamma(theta+eps)
            t3 = tf.lgamma(y_true+1.0)
            t4 = -(theta * (tf.log(theta+eps)))
            t5 = -(y_true * (tf.log(y_pred+eps)))
            t6 = (theta+y_true) * tf.log(theta+y_pred+eps)

            assert_ops = [
                    tf.verify_tensor_all_finite(y_pred, 'y_pred has inf/nans'),
                    tf.verify_tensor_all_finite(t1, 't1 has inf/nans'),
                    tf.verify_tensor_all_finite(t2, 't2 has inf/nans'),
                    tf.verify_tensor_all_finite(t3, 't3 has inf/nans'),
                    tf.verify_tensor_all_finite(t4, 't4 has inf/nans'),
                    tf.verify_tensor_all_finite(t5, 't5 has inf/nans'),
                    tf.verify_tensor_all_finite(t6, 't6 has inf/nans')]

            if self.debug:
                tf.summary.histogram('t1', t1)
                tf.summary.histogram('t2', t2)
                tf.summary.histogram('t3', t3)
                tf.summary.histogram('t4', t4)
                tf.summary.histogram('t5', t5)
                tf.summary.histogram('t6', t6)

                with tf.control_dependencies(assert_ops):
                    final = t1 + t2 + t3 + t4 + t5 + t6

            else:
                final = t1 + t2 + t3 + t4 + t5 + t6

            if reduce:
                if self.masking:
                    final = tf.divide(tf.reduce_sum(final), nelem)
                else:
                    final = tf.reduce_mean(final)


        return final


class ConstantDispersionLayer(Layer):
    '''
        An identity layer which allows us to inject extra parameters
        such as dispersion to Keras models
    '''
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def build(self, input_shape):
        self.theta = self.add_weight(shape=(1, input_shape[1]),
                                     initializer='zeros',
                                     trainable=True)
        self.theta_exp = 1.0/(K.exp(self.theta)+1e-10)
        super().build(input_shape)

    def call(self, x):
        return tf.identity(x)

    def compute_output_shape(self, input_shape):
        return input_shape


class ZINB(NB):
    def __init__(self, pi, scope='zinb_loss/', **kwargs):
        super().__init__(scope=scope, **kwargs)
        self.pi = pi

    def loss(self, y_true, y_pred):
        scale_factor = self.scale_factor
        eps = self.eps

        with tf.name_scope(self.scope):
            # reuse existing NB neg.log.lik.
            nb_case = super().loss(y_true, y_pred, reduce=False) - tf.log(1.0-self.pi+eps)

            y_true = tf.cast(y_true, tf.float32)
            y_pred = tf.cast(y_pred, tf.float32) * scale_factor
            theta = 1.0/(self.theta+eps)

            zero_nb = tf.pow(theta/(theta+y_pred+eps), theta)
            zero_case = -tf.log(self.pi + ((1.0-self.pi)*zero_nb)+eps)
            result = tf.where(tf.less(y_true, 1e-8), zero_case, nb_case)

            if self.masking:
                result = _reduce_mean(result)
            else:
                result = tf.reduce_mean(result)

            if self.debug:
                tf.summary.histogram('nb_case', nb_case)
                tf.summary.histogram('zero_nb', zero_nb)
                tf.summary.histogram('zero_case', zero_case)

        return result
