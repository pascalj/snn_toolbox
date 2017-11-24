# -*- coding: utf-8 -*-
"""INI spiking neuron simulator.

This module defines the layer objects used to create a spiking neural network
for our built-in INI simulator
:py:mod:`~snntoolbox.simulation.target_simulators.INI_target_sim`.

@author: rbodo
"""

from __future__ import division, absolute_import
from __future__ import print_function, unicode_literals

import numpy as np
from future import standard_library
from keras import backend as k
from keras.layers import Dense, Flatten, AveragePooling2D, MaxPooling2D, Conv2D
from keras.layers import Layer, Concatenate

standard_library.install_aliases()


class SpikeLayer(Layer):
    """Base class for layer with spiking neurons."""

    def __init__(self, **kwargs):
        self.config = kwargs.pop(str('config'), None)
        self.layer_type = self.class_name
        self.batch_size = self.config.getint('simulation', 'batch_size')
        self.dt = self.config.getfloat('simulation', 'dt')
        self.duration = self.config.getint('simulation', 'duration')
        self.tau_refrac = self.config.getfloat('cell', 'tau_refrac')
        self._v_thresh = self.config.getfloat('cell', 'v_thresh')
        self.v_thresh = None
        self.time = None
        self.mem = self.spiketrain = self.impulse = None
        self.refrac_until = None
        self._kernel = self._bias = None
        self.last_spiketimes = None
        self.prospective_spikes = None
        self.missing_impulse = None

        allowed_kwargs = {'input_shape',
                          'batch_input_shape',
                          'batch_size',
                          'dtype',
                          'name',
                          'trainable',
                          'weights',
                          'input_dtype',  # legacy
                          }
        for kwarg in kwargs.copy():
            if kwarg not in allowed_kwargs:
                kwargs.pop(kwarg)
        Layer.__init__(self, **kwargs)
        self.stateful = True

    def reset(self, sample_idx):
        """Reset layer variables."""

        self.reset_spikevars(sample_idx)

    @property
    def class_name(self):
        """Get class name."""

        return self.__class__.__name__

    def update_neurons(self):
        """Update neurons according to activation function."""

        # Update membrane potentials.
        new_mem = self.get_new_mem()

        # Generate spikes.
        output_spikes = self.linear_activation(new_mem)

        # Reset membrane potential after spikes.
        self.set_reset_mem(new_mem, output_spikes)

        # Store refractory period after spikes.
        new_refrac = k.tf.where(k.not_equal(output_spikes, 0),
                                k.ones_like(output_spikes) *
                                (self.time + self.tau_refrac),
                                self.refrac_until)
        c = new_refrac[:self.batch_size]
        cc = k.concatenate([c, c], 0)
        updates = [k.tf.assign(self.refrac_until, cc)]

        if self.spiketrain is not None:
            c = self.time * k.cast(k.not_equal(output_spikes, 0),
                                   k.floatx())[:self.batch_size]
            cc = k.concatenate([c, c], 0)
            updates += [k.tf.assign(self.spiketrain, cc)]

        with k.tf.control_dependencies(updates):
            masked_impulse = k.tf.where(k.greater(self.refrac_until, self.time),
                                        k.zeros_like(self.impulse),
                                        self.impulse)
            c = k.greater(masked_impulse, 0)[:self.batch_size]
            cc = k.cast(k.concatenate([c, c], 0), k.floatx())
            updates = [k.tf.assign(self.prospective_spikes, cc)]
            new_thresh = self._v_thresh * k.ones_like(self.v_thresh) + \
                self.missing_impulse
            updates += [k.tf.assign(self.v_thresh, new_thresh)]

            with k.tf.control_dependencies(updates):
                # Compute post-synaptic potential.
                psp = self.get_psp(output_spikes)

                return k.cast(psp, k.floatx())

    def linear_activation(self, mem):
        """Linear activation."""
        return k.cast(k.greater_equal(mem, self.v_thresh), k.floatx())

    def get_new_mem(self):
        """Add input to membrane potential."""

        # Destroy impulse if in refractory period
        masked_impulse = self.impulse if self.tau_refrac == 0 else \
            k.tf.where(k.greater(self.refrac_until, self.time),
                       k.zeros_like(self.impulse), self.impulse)

        new_mem = self.mem + masked_impulse

        if self.config.getboolean('cell', 'leak'):
            # Todo: Implement more flexible version of leak!
            new_mem = k.tf.where(k.greater(new_mem, 0), new_mem - 0.1 * self.dt,
                                 new_mem)

        return new_mem

    def set_reset_mem(self, mem, spikes):
        """
        Reset membrane potential ``mem`` array where ``spikes`` array is
        nonzero.
        """

        new = k.tf.where(k.not_equal(spikes, 0), k.zeros_like(mem), mem)
        self.add_update([(self.mem, new)])

    def get_psp(self, output_spikes):
        new_spiketimes = k.tf.where(
            k.not_equal(output_spikes, 0),
            k.ones_like(output_spikes) * self.time,
            self.last_spiketimes)
        assign_new_spiketimes = k.tf.assign(self.last_spiketimes,
                                            new_spiketimes)
        with k.tf.control_dependencies([assign_new_spiketimes]):
            last_spiketimes = self.last_spiketimes + 0  # Dummy op
            # psp = k.maximum(0., k.tf.divide(self.dt, last_spiketimes))
            psp = k.tf.where(k.greater(last_spiketimes, 0),
                             k.ones_like(output_spikes) * self.dt,
                             k.zeros_like(output_spikes))
        return psp

    def get_time(self):
        """Get simulation time variable.

            Returns
            -------

            time: float
                Current simulation time.
            """

        return k.get_value(self.time)

    def set_time(self, time):
        """Set simulation time variable.

        Parameters
        ----------

        time: float
            Current simulation time.
        """

        k.set_value(self.time, time)

    def init_membrane_potential(self, output_shape=None, mode='zero'):
        """Initialize membrane potential.

        Helpful to avoid transient response in the beginning of the simulation.
        Not needed when reset between frames is turned off, e.g. with a video
        data set.

        Parameters
        ----------

        output_shape: Optional[tuple]
            Output shape
        mode: str
            Initialization mode.

            - ``'uniform'``: Random numbers from uniform distribution in
              ``[-thr, thr]``.
            - ``'bias'``: Negative bias.
            - ``'zero'``: Zero (default).

        Returns
        -------

        init_mem: ndarray
            A tensor of ``self.output_shape`` (same as layer).
        """

        if output_shape is None:
            output_shape = self.output_shape

        if mode == 'uniform':
            init_mem = k.random_uniform(output_shape,
                                        -self._v_thresh, self._v_thresh)
        elif mode == 'bias':
            init_mem = np.zeros(output_shape, k.floatx())
            if hasattr(self, 'b'):
                b = self.get_weights()[1]
                for i in range(len(b)):
                    init_mem[:, i, Ellipsis] = -b[i]
        else:  # mode == 'zero':
            init_mem = np.zeros(output_shape, k.floatx())
        return init_mem

    def reset_spikevars(self, sample_idx):
        """
        Reset variables present in spiking layers. Can be turned off for
        instance when a video sequence is tested.
        """

        mod = self.config.getint('simulation', 'reset_between_nth_sample')
        mod = mod if mod else sample_idx + 1
        do_reset = sample_idx % mod == 0
        if do_reset:
            k.set_value(self.mem, self.init_membrane_potential())
        k.set_value(self.time, np.float32(self.dt))
        zeros_output_shape = np.zeros(self.output_shape, k.floatx())
        if self.tau_refrac > 0:
            k.set_value(self.refrac_until, zeros_output_shape)
        if self.spiketrain is not None:
            k.set_value(self.spiketrain, zeros_output_shape)
        k.set_value(self.last_spiketimes, zeros_output_shape - 1)
        k.set_value(self.v_thresh, zeros_output_shape + self._v_thresh)
        k.set_value(self.prospective_spikes, zeros_output_shape)
        k.set_value(self.missing_impulse, zeros_output_shape)

    def init_neurons(self, input_shape):
        """Init layer neurons."""

        from snntoolbox.bin.utils import get_log_keys, get_plot_keys

        output_shape = self.compute_output_shape(input_shape)
        self.v_thresh = k.variable(self._v_thresh)
        self.mem = k.variable(self.init_membrane_potential(output_shape))
        self.time = k.variable(self.dt)
        # To save memory and computations, allocate only where needed:
        if self.tau_refrac > 0:
            self.refrac_until = k.zeros(output_shape)
        if any({'spiketrains', 'spikerates', 'correlation', 'spikecounts',
                'hist_spikerates_activations', 'operations',
                'synaptic_operations_b_t', 'neuron_operations_b_t',
                'spiketrains_n_b_l_t'} & (get_plot_keys(self.config) |
               get_log_keys(self.config))):
            self.spiketrain = k.zeros(output_shape)
        self.last_spiketimes = k.variable(-np.ones(output_shape))
        self.v_thresh = k.variable(self._v_thresh * np.ones(output_shape))
        self.prospective_spikes = k.variable(np.zeros(output_shape))
        self.missing_impulse = k.variable(np.zeros(output_shape))

    def get_layer_idx(self):
        """Get index of layer."""

        label = self.name.split('_')[0]
        layer_idx = None
        for i in range(len(label)):
            if label[:i].isdigit():
                layer_idx = int(label[:i])
        return layer_idx


def spike_call(call):
    def decorator(self, x):

        updates = []
        if len(self.weights) > 0:
            store_old_kernel = k.tf.assign(self._kernel, self.kernel)
            store_old_bias = k.tf.assign(self._bias, self.bias)
            updates += [store_old_kernel, store_old_bias]
            with k.tf.control_dependencies(updates):
                new_kernel = k.abs(self.kernel)
                new_bias = k.zeros_like(self.bias)
                assign_new_kernel = k.tf.assign(self.kernel, new_kernel)
                assign_new_bias = k.tf.assign(self.bias, new_bias)
                updates += [assign_new_kernel, assign_new_bias]
            with k.tf.control_dependencies(updates):
                c = call(self, x)[self.batch_size:]
                cc = k.concatenate([c, c], 0)
                updates = [k.tf.assign(self.missing_impulse, cc)]
                with k.tf.control_dependencies(updates):
                    updates = [k.tf.assign(self.kernel, self._kernel),
                               k.tf.assign(self.bias, self._bias)]
        elif 'AveragePooling' in self.name:
            c = call(self, x)[self.batch_size:]
            cc = k.concatenate([c, c], 0)
            updates = [k.tf.assign(self.missing_impulse, cc)]
        else:
            updates = []

        with k.tf.control_dependencies(updates):
            # Only call layer if there are input spikes. This is to prevent
            # accumulation of bias.
            self.impulse = k.tf.cond(k.any(k.not_equal(x[:self.batch_size], 0)),
                                     lambda: call(self, x),
                                     lambda: k.zeros_like(self.mem))
            psp = self.update_neurons()[:self.batch_size]

        return k.concatenate([psp,
                              self.prospective_spikes[self.batch_size:]], 0)

    return decorator


class SpikeConcatenate(Concatenate):
    """Spike merge layer"""

    def __init__(self, axis, **kwargs):
        kwargs.pop(str('config'))
        Concatenate.__init__(self, axis, **kwargs)

    def _merge_function(self, inputs):
        return self._merge_function(inputs)

    @staticmethod
    def get_time():

        pass

    @staticmethod
    def reset(sample_idx):
        """Reset layer variables."""

        pass

    @property
    def class_name(self):
        """Get class name."""

        return self.__class__.__name__


class SpikeFlatten(Flatten):
    """Spike flatten layer."""

    def __init__(self, **kwargs):
        self.config = kwargs.pop(str('config'), None)
        self.batch_size = self.config.getint('simulation', 'batch_size')
        Flatten.__init__(self, **kwargs)

    def call(self, x, mask=None):

        psp = k.cast(Flatten.call(self, x), k.floatx())

        prospective_spikes = Flatten.call(self, x)

        return k.concatenate([psp[:self.batch_size],
                              prospective_spikes[self.batch_size:]], 0)

    @staticmethod
    def get_time():
        return None

    def reset(self, sample_idx):
        """Reset layer variables."""

        pass

    @property
    def class_name(self):
        """Get class name."""

        return self.__class__.__name__


class SpikeDense(Dense, SpikeLayer):
    """Spike Dense layer."""

    def build(self, input_shape):
        """Creates the layer neurons and connections.

        Parameters
        ----------

        input_shape: Union[list, tuple, Any]
            Keras tensor (future input to layer) or list/tuple of Keras tensors
            to reference for weight shape computations.
        """

        Dense.build(self, input_shape)
        self.init_neurons(input_shape)
        self._kernel = k.variable(k.zeros_like(self.kernel))
        self._bias = k.variable(k.zeros_like(self.bias))

    @spike_call
    def call(self, x, **kwargs):

        return Dense.call(self, x)


class SpikeConv2D(Conv2D, SpikeLayer):
    """Spike 2D Convolution."""

    def build(self, input_shape):
        """Creates the layer weights.
        Must be implemented on all layers that have weights.

        Parameters
        ----------

        input_shape: Union[list, tuple, Any]
            Keras tensor (future input to layer) or list/tuple of Keras tensors
            to reference for weight shape computations.
        """

        Conv2D.build(self, input_shape)
        self.init_neurons(input_shape)
        self._kernel = k.variable(k.zeros_like(self.kernel))
        self._bias = k.variable(k.zeros_like(self.bias))

    @spike_call
    def call(self, x, mask=None):

        return Conv2D.call(self, x)


class SpikeAveragePooling2D(AveragePooling2D, SpikeLayer):
    """Average Pooling."""

    def build(self, input_shape):
        """Creates the layer weights.
        Must be implemented on all layers that have weights.

        Parameters
        ----------

        input_shape: Union[list, tuple, Any]
            Keras tensor (future input to layer) or list/tuple of Keras tensors
            to reference for weight shape computations.
        """

        AveragePooling2D.build(self, input_shape)
        self.init_neurons(input_shape)

    @spike_call
    def call(self, x, mask=None):

        return AveragePooling2D.call(self, x)


class SpikeMaxPooling2D(MaxPooling2D, SpikeLayer):
    """Spiking Max Pooling."""

    def build(self, input_shape):
        """Creates the layer neurons and connections..

        Parameters
        ----------

        input_shape: Union[list, tuple, Any]
            Keras tensor (future input to layer) or list/tuple of Keras tensors
            to reference for weight shape computations.
        """

        MaxPooling2D.build(self, input_shape)
        self.init_neurons(input_shape)

    @spike_call
    def call(self, x, mask=None):
        """Layer functionality."""

        return MaxPooling2D.call(self, x)


custom_layers = {'SpikeFlatten': SpikeFlatten,
                 'SpikeDense': SpikeDense,
                 'SpikeConv2D': SpikeConv2D,
                 'SpikeAveragePooling2D': SpikeAveragePooling2D,
                 'SpikeMaxPooling2D': SpikeMaxPooling2D,
                 'SpikeConcatenate': SpikeConcatenate}
