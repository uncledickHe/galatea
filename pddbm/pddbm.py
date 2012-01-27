__authors__ = "Ian Goodfellow"
__copyright__ = "Copyright 2011, Universite de Montreal"
__credits__ = ["Ian Goodfellow"]
__license__ = "3-clause BSD"
__maintainer__ = "Ian Goodfellow"

import time
from pylearn2.models.model import Model
from theano import config, function, shared
import theano.tensor as T
import numpy as np
import warnings
from theano.gof.op import get_debug_values, debug_error_message
from pylearn2.utils import make_name, as_floatX

warnings.warn('pddbm changing the recursion limit')
import sys
sys.setrecursionlimit(50000)

from pylearn2.models.s3c import full_min
from pylearn2.models.s3c import full_max
from pylearn2.models.s3c import reflection_clip
from pylearn2.models.s3c import damp
from pylearn2.models.s3c import S3C
from pylearn2.models.s3c import SufficientStatistics
from theano.printing import min_informative_str
from theano.printing import Print
from theano.gof.op import debug_assert
from theano.gof.op import get_debug_values

warnings.warn('There is a known bug where for some reason the w field of s3c '
'gets serialized. Not sure if other things get serialized too but be sure to '
'call make_pseudoparams on everything you unpickle. I wish theano was not '
'such a piece of shit!')

def flatten(collection):
    rval = set([])

    for elem in collection:
        if hasattr(elem,'__len__'):
            rval = rval.union(flatten(elem))
        else:
            rval = rval.union([elem])

    return rval


class PDDBM(Model):

    """ Implements a model of the form
        P(v,s,h,g[0],...,g[N_g]) = S3C(v,s|h)DBM(h,g)
    """

    def __init__(self,
            s3c,
            dbm,
            inference_procedure,
            learning_rate = 1e-3,
            h_penalty = 0.0,
            h_target = None,
            g_penalties = None,
            g_targets = None,
            print_interval = 10000,
            dbm_weight_decay = None):
        """
            s3c: a galatea.s3c.s3c.S3C object
                will become owned by the PDDBM
                it won't be deleted but many of its fields will change
            dbm: a pylearn2.dbm.DBM object
                will become owned by the PDDBM
                it won't be deleted but many of its fields will change
            inference_procedure: a galatea.pddbm.pddbm.InferenceProcedure
            h_bias_src: 's3c' to take bias on h from s3c, 'dbm' to take it from dbm
            print_interval: number of examples between each status printout
        """

        super(PDDBM,self).__init__()

        self.learning_rate = learning_rate

        self.dbm_weight_decay = dbm_weight_decay

        self.s3c = s3c

        if self.s3c.m_step is not None:
            self.B_learning_rate_scale = self.s3c.m_step.B_learning_rate_scale
            self.W_learning_rate_scale = self.s3c.m_step.W_learning_rate_scale

        s3c.m_step = None
        self.dbm = dbm

        self.rng = np.random.RandomState([1,2,3])

        #must use DBM bias now
        assert dbm.bias_vis.get_value(borrow=True).shape \
                == s3c.bias_hid.get_value(borrow=True).shape
        self.s3c.bias_hid = self.dbm.bias_vis

        self.nvis = s3c.nvis

        #don't support some exotic options on s3c
        for option in ['monitor_functional',
                       'recycle_q',
                       'debug_m_step']:
            if getattr(s3c, option):
                warnings.warn('PDDBM does not support '+option+' in '
                        'the S3C layer, disabling it')
                setattr(s3c, option, False)

        self.print_interval = print_interval

        s3c.print_interval = None
        dbm.print_interval = None

        inference_procedure.register_model(self)
        self.inference_procedure = inference_procedure

        self.num_g = len(self.dbm.W)

        self.h_penalty = h_penalty
        self.h_target = h_target

        self.g_penalties = g_penalties
        self.g_targets = g_targets

        self.redo_everything()


    def get_weights(self):
        x = input('which weights you want?')

        if x == 2:
            warnings.warn("HACK!")
            return np.dot(self.s3c.W.get_value() \
                    * self.s3c.mu.get_value(), self.dbm.W[0].get_value())

        if x == 1:
            return self.s3c.W.get_value()

        assert False

    def redo_everything(self):

        #we don't call redo_everything on s3c because this would reset its weights
        #however, calling redo_everything on the dbm just resets its negative chain
        self.dbm.redo_everything()

        self.test_batch_size = 2

        self.redo_theano()


    def get_monitoring_channels(self, V):
            try:
                self.compile_mode()

                self.s3c.set_monitoring_channel_prefix('s3c_')

                rval = self.s3c.get_monitoring_channels(V)

                from_inference_procedure = self.inference_procedure.get_monitoring_channels(V)

                rval.update(from_inference_procedure)


                self.dbm.set_monitoring_channel_prefix('dbm_')

                from_dbm = self.dbm.get_monitoring_channels(V)

                rval.update(from_dbm)

            finally:
                self.deploy_mode()

            return rval


    def compile_mode(self):
        """ If any shared variables need to have batch-size dependent sizes,
        sets them all to the sizes used for interactive debugging during graph construction """
        pass

    def deploy_mode(self):
        """ If any shared variables need to have batch-size dependent sizes, sets them all to their runtime sizes """
        pass

    def get_params(self):
        return list(set(self.s3c.get_params()).union(set(self.dbm.get_params())))

    def make_learn_func(self, V):
        """
        V: a symbolic design matrix
        """

        #run variational inference on the train set
        hidden_obs = self.inference_procedure.infer(V)

        #make a restricted dictionary containing only vars s3c knows about
        restricted_obs = {}
        for key in hidden_obs:
            if key != 'G_hat':
                restricted_obs[key] = hidden_obs[key]


        #request s3c sufficient statistics
        needed_stats = \
         S3C.expected_log_prob_v_given_hs_needed_stats().union(\
         S3C.expected_log_prob_s_given_h_needed_stats())
        stats = SufficientStatistics.from_observations(needed_stats = needed_stats,
                V = V, **restricted_obs)

        #don't backpropagate through inference
        obs_set = set(hidden_obs.values())
        stats_set = set(stats.d.values())
        constants = flatten(obs_set.union(stats_set))

        G_hat = hidden_obs['G_hat']
        for i, G in enumerate(G_hat):
            G.name = 'final_G_hat[%d]' % (i,)
        H_hat = hidden_obs['H_hat']
        H_hat.name = 'final_H_hat'
        S_hat = hidden_obs['S_hat']
        S_hat.name = 'final_S_hat'

        assert H_hat in constants
        for G in G_hat:
            assert G in constants
        assert S_hat in constants

        expected_log_prob_v_given_hs = self.s3c.expected_log_prob_v_given_hs(stats, \
                H_hat = H_hat, S_hat = S_hat)
        assert len(expected_log_prob_v_given_hs.type.broadcastable) == 0


        expected_log_prob_s_given_h  = self.s3c.expected_log_prob_s_given_h(stats)
        assert len(expected_log_prob_s_given_h.type.broadcastable) == 0


        expected_dbm_energy = self.dbm.expected_energy( V_hat = H_hat, H_hat = G_hat )
        assert len(expected_dbm_energy.type.broadcastable) == 0

        test = T.grad(expected_dbm_energy, self.dbm.W[0], consider_constant = constants)

        #note: this is not the complete tractable part of the objective
        #the objective also includes the entropy of Q, but we drop that since it is
        #not a function of the parameters and we're not able to compute the true
        #value of the objective function anyway
        tractable_obj = expected_log_prob_v_given_hs + \
                        expected_log_prob_s_given_h  - \
                        expected_dbm_energy

        assert len(tractable_obj.type.broadcastable) == 0


        if self.dbm_weight_decay:

            for i, t in enumerate(zip(self.dbm_weight_decay, self.dbm.W)):

                coeff, W = t

                coeff = as_floatX(float(coeff))
                coeff = T.as_tensor_variable(coeff)
                coeff.name = 'dbm_weight_decay_coeff_'+str(i)

                tractable_obj = tractable_obj - coeff * T.mean(T.sqr(W))

        if self.h_penalty != 0.0:
            next_h = self.inference_procedure.infer_H_hat(V = V,
                H_hat = H_hat, S_hat = S_hat, G1_hat = G_hat[0])

            err = H_hat - self.h_target

            abs_err = abs(err)

            penalty = T.mean(abs_err)

            tractable_obj =  tractable_obj - self.h_penalty * penalty

        if self.g_penalties is not None:
            for i in xrange(len(self.dbm.bias_hid)):
                G = self.inference_procedure.infer_G_hat(H_hat = H_hat, G_hat = G_hat, idx = i)

                g = T.mean(G,axis=0)

                err = g - self.g_targets[i]

                abs_err = abs(err)

                penalty = T.mean(abs_err)

                tractable_obj = tractable_obj - self.g_penalties[i] * penalty

        assert len(tractable_obj.type.broadcastable) == 0

        #take the gradient of the tractable part
        params = self.get_params()
        grads = T.grad(tractable_obj, params, consider_constant = constants, disconnected_inputs = 'warn')

        #put gradients into convenient dictionary
        params_to_grads = {}
        for param, grad in zip(params, grads):
            params_to_grads[param] = grad

        #add the approximate gradients
        params_to_approx_grads = self.dbm.get_neg_phase_grads()

        for param in params_to_approx_grads:
            params_to_grads[param] = params_to_grads[param] + params_to_approx_grads[param]

        learning_updates = self.get_param_updates(params_to_grads)

        sampling_updates = self.dbm.get_sampling_updates()

        for key in sampling_updates:
            learning_updates[key] = sampling_updates[key]

        self.censor_updates(learning_updates)

        print "compiling function..."
        t1 = time.time()
        rval = function([V], updates = learning_updates)
        t2 = time.time()
        print "... compilation took "+str(t2-t1)+" seconds"
        print "graph size: ",len(rval.maker.env.toposort())

        return rval

    def get_param_updates(self, params_to_grads):

        warnings.warn("TODO: get_param_updates does not use geodesics for now")

        rval = {}

        learning_rate = {}

        for param in params_to_grads:
            if param is self.s3c.B_driver:
                learning_rate[param] = as_floatX(self.learning_rate * self.B_learning_rate_scale)
            elif param is self.s3c.W:
                learning_rate[param] = as_floatX(self.learning_rate * self.W_learning_rate_scale)
            else:
                learning_rate[param] = as_floatX(self.learning_rate)

        for key in params_to_grads:
            rval[key] = key + learning_rate[key] * params_to_grads[key]

        for param in self.get_params():
            assert param in params_to_grads

        return rval

    def censor_updates(self, updates):

        self.s3c.censor_updates(updates)
        self.dbm.censor_updates(updates)

    def random_design_matrix(self, batch_size, theano_rng):

        if not hasattr(self,'p'):
            self.make_pseudoparams()

        H_sample = self.dbm.random_design_matrix(batch_size, theano_rng)

        V_sample = self.s3c.random_design_matrix(batch_size, theano_rng, H_sample = H_sample)

        return V_sample


    def make_pseudoparams(self):
        self.s3c.make_pseudoparams()

    def redo_theano(self):

        self.s3c.reset_censorship_cache()

        try:
            self.compile_mode()

            init_names = dir(self)

            self.make_pseudoparams()

            X = T.matrix(name='V')
            X.tag.test_value = np.cast[config.floatX](self.rng.randn(self.test_batch_size,self.nvis))


            self.s3c.e_step.register_model(self.s3c)

            self.learn_func = self.make_learn_func(X)

            final_names = dir(self)

            self.register_names_to_del([name for name in final_names if name not in init_names])
        finally:
            self.deploy_mode()
    #

    def learn(self, dataset, batch_size):
        self.learn_mini_batch(dataset.get_batch_design(batch_size))

    def learn_mini_batch(self, X):


        assert self.s3c is self.inference_procedure.s3c_e_step.model

        self.learn_func(X)

        if self.monitor.examples_seen % self.print_interval == 0:
            print ""
            print "S3C:"
            self.s3c.print_status()
            print "DBM:"
            self.dbm.print_status()

    def get_weights_format(self):
        return self.s3c.get_weights_format()

class InferenceProcedure:
    """

    Variational inference

    """

    def __init__(self, schedule,
                       clip_reflections = False,
                       monitor_kl = False,
                       rho = 0.5):
        """Parameters
        --------------
        schedule:
            list of steps. each step can consist of one of the following:
                ['s', <new_coeff>] where <new_coeff> is a number between 0. and 1.
                    does a damped parallel update of s, putting <new_coeff> on the new value of s
                ['h', <new_coeff>] where <new_coeff> is a number between 0. and 1.
                    does a damped parallel update of h, putting <new_coeff> on the new value of h
                ['g', idx]
                    does a block update of g[idx]

        clip_reflections, rho : if clip_reflections is true, the update to Mu1[i,j] is
            bounded on one side by - rho * Mu1[i,j] and unbounded on the other side
        """

        self.schedule = schedule

        self.clip_reflections = clip_reflections
        self.monitor_kl = monitor_kl

        self.rho = as_floatX(rho)

        self.model = None



    def get_monitoring_channels(self, V):

        rval = {}

        obs_history = self.infer(V, return_history = True)

        if self.monitor_kl:

            for i in xrange(1, 2 + len(self.schedule)):
                obs = obs_history[i-1]

                if i == 1:
                    summary = '(init)'
                else:
                    step = self.schedule[i-2]
                    summary = '(' + step[0]+','+step[1]+')'

                rval['trunc_KL_'+str(i)+summary] = self.truncated_KL(V, obs).mean()

        final_vals = obs_history[-1]

        H_hat = final_vals['H_hat']
        h = T.mean(H_hat, axis=0)

        rval['h_min'] = full_min(h)
        rval['h_mean'] = T.mean(h)
        rval['h_max'] = full_max(h)

        Gs = final_vals['G_hat']

        for i, G in enumerate(Gs):

            g = T.mean(G,axis=0)

            rval['g[%d]_min'%(i,)] = full_min(g)
            rval['g[%d]_mean'%(i,)] = T.mean(g)
            rval['g[%d]_max'%(i,)] = full_max(g)

        return rval

    def register_model(self, model):
        self.model = model

        self.s3c_e_step = self.model.s3c.e_step

        self.s3c_e_step.clip_reflections = self.clip_reflections
        self.s3c_e_step.rho = self.rho

        self.dbm_ip = self.model.dbm.inference_procedure


    def dbm_observations(self, obs):

        rval = {}
        rval['H_hat'] = obs['G_hat']
        rval['V_hat'] = obs['H_hat']

        return rval

    def truncated_KL(self, V, obs):
        """ KL divergence between variational and true posterior, dropping terms that don't
            depend on the variational parameters """

        s3c_truncated_KL = self.s3c_e_step.truncated_KL(V, obs).mean()

        dbm_obs = self.dbm_observations(obs)

        dbm_truncated_KL = self.dbm_ip.truncated_KL(V = obs['H_hat'], obs = dbm_obs)
        assert len(dbm_truncated_KL.type.broadcastable) == 0

        for s3c_kl_val, dbm_kl_val in get_debug_values(s3c_truncated_KL, dbm_truncated_KL):
            debug_assert( not np.any(np.isnan(s3c_kl_val)))
            debug_assert( not np.any(np.isnan(dbm_kl_val)))

        warnings.warn("""TODO: double check that this decomposition works--
                        It may be ignoring a subtlety where the math for dbm.truncated_kl is based on
                        a fixed V but the pddbm is actually passing it variational parameters
                        for distributional V""")

        rval = s3c_truncated_KL + dbm_truncated_KL

        return rval

    def infer_H_hat(self, V, H_hat, S_hat, G1_hat):
        """
            G1_hat: variational parameters for the g layer closest to h
                    here we use the designation from the math rather than from
                    the list, where it is G_hat[0]
        """

        s3c_presigmoid = self.s3c_e_step.infer_H_hat_presigmoid(V, H_hat, S_hat)

        W = self.model.dbm.W[0]

        top_down = T.dot(G1_hat, W.T)

        presigmoid = s3c_presigmoid + top_down

        H = T.nnet.sigmoid(presigmoid)

        return H

    def infer(self, V, return_history = False):
        """

            return_history: if True:
                                returns a list of dictionaries with
                                showing the history of the variational
                                parameters
                                throughout fixed point updates
                            if False:
                                returns a dictionary containing the final
                                variational parameters
        """

        alpha = self.model.s3c.alpha
        s3c_e_step = self.s3c_e_step
        dbm_ip = self.dbm_ip

        var_s0_hat = 1. / alpha
        var_s1_hat = s3c_e_step.infer_var_s1_hat()

        H_hat = s3c_e_step.init_H_hat(V)
        G_hat = dbm_ip.init_H_hat(H_hat)
        S_hat = s3c_e_step.init_S_hat(V)

        H_hat.name = 'init_H_hat'
        S_hat.name = 'init_S_hat'

        for Hv in get_debug_values(H_hat):

            if Hv.shape[1] != s3c_e_step.model.nhid:
                debug_error_message('H prior has wrong # hu, expected %d actual %d'%\
                        (s3c_e_step.model.nhid,
                            Hv.shape[1]))

        for Sv in get_debug_values(S_hat):
            if isinstance(Sv, tuple):
                warnings.warn("I got a tuple from this and I have no idea why the fuck that happens. Pulling out the single element of the tuple")
                Sv ,= Sv

            if Sv.shape[1] != s3c_e_step.model.nhid:
                debug_error_message('prior has wrong # hu, expected %d actual %d'%\
                        (s3c_e_step.model.nhid,
                            Sv.shape[1]))

        def check_H(my_H, my_V):
            if my_H.dtype != config.floatX:
                raise AssertionError('my_H.dtype should be config.floatX, but they are '
                        ' %s and %s, respectively' % (my_H.dtype, config.floatX))

            allowed_v_types = ['float32']

            if config.floatX == 'float64':
                allowed_v_types.append('float64')

            assert my_V.dtype in allowed_v_types

            if config.compute_test_value != 'off':
                from theano.gof.op import PureOp
                Hv = PureOp._get_test_value(my_H)

                Vv = my_V.tag.test_value

                assert Hv.shape[0] == Vv.shape[0]

        check_H(H_hat,V)

        def make_dict():

            return {
                    'G_hat' : tuple(G_hat),
                    'H_hat' : H_hat,
                    'S_hat' : S_hat,
                    'var_s0_hat' : var_s0_hat,
                    'var_s1_hat': var_s1_hat,
                    }

        history = [ make_dict() ]

        for i, step in enumerate(self.schedule):

            letter, number = step

            coeff = as_floatX(number)

            coeff = T.as_tensor_variable(coeff)

            coeff.name = 'coeff_step_'+str(i)

            if letter == 's':

                new_S_hat = s3c_e_step.infer_S_hat(V, H_hat, S_hat)
                new_S_hat.name = 'new_S_hat_step_'+str(i)

                if self.clip_reflections:
                    clipped_S_hat = reflection_clip(S_hat = S_hat, new_S_hat = new_S_hat, rho = self.rho)
                else:
                    clipped_S_hat = new_S_hat

                S_hat = damp(old = S_hat, new = clipped_S_hat, new_coeff = coeff)

                S_hat.name = 'S_hat_step_'+str(i)

            elif letter == 'h':

                new_H = self.infer_H_hat(V = V, H_hat = H_hat, S_hat = S_hat, G1_hat = G_hat[0])

                new_H.name = 'new_H_step_'+str(i)

                H_hat = damp(old = H_hat, new = new_H, new_coeff = coeff)
                H_hat.name = 'new_H_hat_step_'+str(i)

                check_H(H_hat,V)

            elif letter == 'g':

                if not isinstance(number, int):
                    raise ValueError("Step parameter for 'g' code must be an integer in [0, # g layers) "
                            "but got "+str(number)+" (of type "+str(type(number)))


                G_hat[number] = self.infer_G_hat( H_hat = H_hat, G_hat = G_hat, idx = number)

            else:
                raise ValueError("Invalid inference step code '"+letter+"'. Valid options are 's','h' and 'g'.")

            history.append(make_dict())

        if return_history:
            return history
        else:
            return history[-1]

    def infer_G_hat(self, H_hat, G_hat, idx):
        number = idx
        dbm_ip = self.model.dbm.inference_procedure

        b = self.model.dbm.bias_hid[number]

        W = self.model.dbm.W

        W_below = W[number]

        if number == 0:
            H_hat_below = H_hat
        else:
            H_hat_below = G_hat[number - 1]

        num_g = self.model.num_g

        if number == num_g - 1:
            return dbm_ip.infer_H_hat_one_sided(other_H_hat = H_hat_below, W = W_below, b = b)
        else:
            H_hat_above = G_hat[number + 1]
            W_above = W[number+1]
            return dbm_ip.infer_H_hat_two_sided(H_hat_below = H_hat_below, H_hat_above = H_hat_above,
                                   W_below = W_below, W_above = W_above,
                                   b = b)



