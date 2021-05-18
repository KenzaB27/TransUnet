from os import name
import tensorflow as tf 
import tensorflow_addons as tfa
from os.path import join as pjoin
import numpy as np

tfk = tf.keras 
tfkl = tfk.layers



class StdConv2D(tfkl.Conv2D):
    # def __init__(self, filters, kernel_size, strides, padding, data_format, dilation_rate, groups, activation, use_bias, kernel_initializer, bias_initializer, kernel_regularizer, bias_regularizer, activity_regularizer, kernel_constraint, bias_constraint, name,**kwargs):
    #     super().__init__(filters, kernel_size, strides=strides, padding=padding, data_format=data_format, dilation_rate=dilation_rate, groups=groups, activation=activation, use_bias=use_bias, kernel_initializer=kernel_initializer, bias_initializer=bias_initializer, kernel_regularizer=kernel_regularizer, bias_regularizer=bias_regularizer, activity_regularizer=activity_regularizer, kernel_constraint=kernel_constraint, bias_constraint=bias_constraint, name=name,**kwargs)
    
    def call(self, x):
        w = self.weights[0]
        m, v = tf.nn.moments(w, axes=[0,1,2], keepdims=True) 
        w = (w-m) / tf.sqrt(v+1e-5)
        return tf.nn.conv2d(x, w, self.strides, self.padding.upper(), "NHWC" if self.data_format == "channels_last" else "NCHW", self.dilation_rate, self.name)

def conv3x3(cout, stride=1, groups=1, bias=False, name=""):
    return StdConv2D(cout, kernel_size=3, strides=stride, padding="same", use_bias=bias, groups=groups, name=name)

def conv1x1(cout, stride=1, groups=1, bias=False, name=""):
    return StdConv2D(cout, kernel_size=1, strides=stride, padding="same", use_bias=bias, groups=groups, name=name)

class PreActBottleneck(tfkl.Layer):
    """Pre-activation (v2) bottleneck block.
    """
    def __init__(self, cin, cout=None, cmid=None, stride=1, name="preact", **kwargs):
        super().__init__(name=name, **kwargs)
        cout = cout or cin
        cmid = cmid or cout//4

        self.gn1 = tfa.layers.GroupNormalization(32, epsilon=1e-6) ## TODO check axis
        self.conv1 = conv1x1(cmid, bias=False)
        self.gn2 = tfa.layers.GroupNormalization(32, epsilon=1e-6)
        self.conv2 = conv3x3(cmid, stride, bias=False)  # Original code has it on conv1!!
        self.gn3 = tfa.layers.GroupNormalization(32, epsilon=1e-6)
        self.conv3 = conv1x1(cout, bias=False)

        if (stride != 1 or cin != cout):
            # Projection also with pre-activation according to paper.
            self.downsample = conv1x1(cout, stride, bias=False)
            
            self.gn_proj = tfa.layers.GroupNormalization(cout, epsilon=1e-5)

    def call(self, x):

        # Residual branch
        residual = x
        if hasattr(self, 'downsample'):
            residual = self.downsample(x)
            residual = self.gn_proj(residual)

        # Unit's branch
        y = tf.nn.relu(self.gn1(self.conv1(x)))
        y = tf.nn.relu(self.gn2(self.conv2(y)))
        y = self.gn3(self.conv3(y))

        y = tf.nn.relu(residual + y)
        return y

    def load_from(self, weights, n_block, n_unit):
        conv1_weight = [weights[f"{n_block}/{n_unit}/conv1/kernel"]]
        conv2_weight = [weights[f"{n_block}/{n_unit}/conv2/kernel"]]
        conv3_weight = [weights[f"{n_block}/{n_unit}/conv3/kernel"]]

        gn1_weight = [np.squeeze(weights[f"{n_block}/{n_unit}/gn1/scale"], axis=(0,1,2)), np.squeeze(weights[f"{n_block}/{n_unit}/gn1/bias"], axis=(0,1,2))]
        gn2_weight = [np.squeeze(weights[f"{n_block}/{n_unit}/gn2/scale"], axis=(0,1,2)), np.squeeze(weights[f"{n_block}/{n_unit}/gn2/bias"], axis=(0,1,2))]
        gn3_weight = [np.squeeze(weights[f"{n_block}/{n_unit}/gn3/scale"], axis=(0,1,2)), np.squeeze(weights[f"{n_block}/{n_unit}/gn3/bias"], axis=(0,1,2))]

        self.conv1.set_weights(conv1_weight)
        self.conv2.set_weights(conv2_weight)
        self.conv3.set_weights(conv3_weight)

        self.gn1.set_weights(gn1_weight)
        self.gn2.set_weights(gn2_weight)
        self.gn3.set_weights(gn3_weight)

        if hasattr(self, 'downsample'):
            proj_conv_weight = [weights[f"{n_block}/{n_unit}/conv_proj/kernel"]]
            proj_gn_weight = [np.squeeze(weights[f"{n_block}/{n_unit}/gn_proj/scale"], axis=(0,1,2)), np.squeeze(weights[f"{n_block}/{n_unit}/gn_proj/bias"], axis=(0,1,2))]

            self.downsample.set_weights(proj_conv_weight)
            self.gn_proj.set_weights(proj_gn_weight)


class ResNetV2(tfkl.Layer):
    """Implementation of Pre-activation (v2) ResNet mode."""

    def __init__(self, block_units, width_factor=1, trainable=True, name="resnet_v2", **kwargs):
        super().__init__(trainable=trainable, name=name, **kwargs)
        self.block_units = block_units
        width = int(64 * width_factor)
        self.width = width

        self.root = tfk.Sequential([
            StdConv2D(width, kernel_size=7, strides=2, use_bias=False, padding="same", name="conv"),
            tfa.layers.GroupNormalization(32, epsilon=1e-6),
            tfkl.ReLU()
        ])

        self.body = [
            tfk.Sequential(
                [PreActBottleneck(cin=width, cout=width*4, cmid=width, name="block1_unit1")] + 
                [PreActBottleneck(cin=width*4, cout=width*4, cmid=width, name=f'block1_unit{i:d}') for i in range(2, block_units[0] + 1)]
            ),
            tfk.Sequential(
                [PreActBottleneck(cin=width*4, cout=width*8, cmid=width*2, stride=2, name="block2_unit1")] + 
                [PreActBottleneck(cin=width*8, cout=width*8, cmid=width*2, name=f'block2_unit{i:d}') for i in range(2, block_units[1] + 1)]
            ),
            tfk.Sequential(
                [PreActBottleneck(cin=width*8, cout=width*16, cmid=width*4, stride=2, name="block3_unit1")] + 
                [PreActBottleneck(cin=width*16, cout=width*16, cmid=width*4, name=f'block3_unit{i:d}') for i in range(2, block_units[2] + 1)]
            )
        ]


    def call(self, x):
        features = []
        in_size = x.shape[1]
        x = self.root(x)
        features.append(x)
        x = tfkl.MaxPool2D(pool_size=3, strides=2, padding="valid")(x)
        for i in range(len(self.body.layers)-1):
            x = self.body[i](x)
            right_size = int(in_size / 4 / (i+1))
            b, h, w, c = x.shape
            if h != right_size:
                pad = right_size - h
                assert pad < 3 and pad > 0, "x {} should {}".format(x.shape, right_size)
                feat = tfkl.ZeroPadding2D(padding=((0,pad), (0,pad)))(x)
            else:
                feat = x
            features.append(feat)
        x = self.body[-1](x)
        return x, features[::-1]

    def load_weights(self, res_weights):
        self.root.layers[0].set_weights([res_weights["conv_root/kernel"]])
        self.root.layers[1].set_weights([np.squeeze(res_weights["gn_root/scale"], axis=(0,1,2)), np.squeeze(res_weights["gn_root/bias"], axis=(0,1,2))])

        for i in range(len(self.block_units)):
            for j in range(self.block_units[i]):
                self.body.layers[i].layers[j].load_from(res_weights, n_block=f"block{i+1}", n_unit=f"unit{j+1}")