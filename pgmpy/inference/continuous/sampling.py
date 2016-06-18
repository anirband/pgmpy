"""
    A collection of methods for sampling from continuous models in pgmpy
"""
from math import sqrt

import numpy as np
try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

from pgmpy.inference.continuous import LeapFrog, BaseGradLogPDF, BaseSimulateHamiltonianDynamics
from pgmpy.utils import _check_1d_array_object, _check_length_equal


class HamiltonianMC(object):
    """
    Class for performing sampling using simple
    Hamiltonian Monte Carlo

    Parameters:
    -----------
    model: An instance pgmpy.models
        Model from which sampling has to be done

    grad_log_pdf: A subclass of pgmpy.inference.continuous.BaseGradLogPDF, defaults to None
        A class to find log and gradient of log distribution for a given assignment
        If None, then will use model.get_gradient_log_pdf

    simulate_dynamics: A subclass of pgmpy.inference.continuous.BaseSimulateHamiltonianDynamics
        A class to propose future values of momentum and position in time by simulating
        Hamiltonian Dynamics

    Public Methods:
    ---------------
    sample()
    generate_sample()

    Example:
    --------
    >>> from pgmpy.inference.continuous import HamiltonianMC as HMC, LeapFrog
    >>> from pgmpy.models import JointGaussianDistribution as JGD
    >>> import numpy as np
    >>> mean = np.array([-3, 4])
    >>> covariance = np.array([[3, 0.7], [0.7, 5]])
    >>> model = JGD(['x', 'y'], mean, covariance)
    >>> sampler = HMC(model=model, grad_log_pdf=None, simulate_dynamics=LeapFrog)
    >>> samples = sampler.sample(initial_pos=np.array([1, 1]), num_samples = 10000,
    ...                          trajectory_length=2, stepsize=0.4)
    >>> samples
    array([(5e-324, 5e-324), (-2.2348941964735225, 4.43066330647519),
           (-2.316454719617516, 7.430291195678112), ...,
           (-1.1443831048872348, 3.573135519428842),
           (-0.2325915892988598, 4.155961788010201),
           (-0.7582492446601238, 3.5416519297297056)], 
          dtype=[('x', '<f8'), ('y', '<f8')])

    >>> samples = np.array([samples[var_name] for var_name in model.variables])
    >>> np.cov(samples)
    array([[ 3.0352818 ,  0.71379304],
           [ 0.71379304,  4.91776713]])
    >>> sampler.accepted_proposals
    9932.0
    >>> sampler.acceptance_rate
    0.9932

    References
    ----------
    R.Neal. Handbook of Markov Chain Monte Carlo,
    chapter 5: MCMC Using Hamiltonian Dynamics.
    CRC Press, 2011.
    """

    def __init__(self, model, grad_log_pdf=None, simulate_dynamics=LeapFrog):

        if grad_log_pdf is not None and not issubclass(grad_log_pdf, BaseGradLogPDF):
            raise TypeError("grad_log_pdf must be an instance of " +
                            "pgmpy.inference.base_continuous.BaseGradLogPDF")

        if not issubclass(simulate_dynamics, BaseSimulateHamiltonianDynamics):
            raise TypeError("split_time must be an instance of " +
                            "pgmpy.inference.base_continuous.BaseSimulateHamiltonianDynamics")

        self.model = model
        self.grad_log_pdf = grad_log_pdf
        self.simulate_dynamics = simulate_dynamics
        self.accepted_proposals = 0.0
        self.acceptance_rate = 0

    def _acceptance_prob(self, position, position_bar, momentum, momentum_bar):
        """
        Returns the acceptance probability for given new position(position) and momentum
        """

        # Parameters to help in evaluating Joint distribution P(position, momentum)
        _, logp = self.model.get_gradient_log_pdf(position, self.grad_log_pdf)
        _, logp_bar = self.model.get_gradient_log_pdf(position_bar, self.grad_log_pdf)

        # acceptance_prob = P(position_bar, momentum_bar)/ P(position, momentum)
        potential_change = logp_bar - logp  # Negative change
        kinetic_change = 0.5 * np.float(np.dot(momentum_bar.T, momentum_bar) - np.dot(momentum.T, momentum))

        return np.exp(potential_change - kinetic_change)  # acceptance probability

    def _find_reasonable_stepsize(self, position, stepsize_app=1):
        """
        Method for choosing initial value of stepsize

        References
        -----------
        Matthew D. Hoffman, Andrew Gelman, The No-U-Turn Sampler: Adaptively
        Setting Path Lengths in Hamiltonian Monte Carlo. Journal of
        Machine Learning Research 15 (2014) 1351-1381
        Algorithm 4 : Heuristic for choosing an initial value of epsilon
        """
        # momentum = N(0, I)
        momentum = np.reshape(np.random.normal(0, 1, len(position)), position.shape)

        # Take a single step in time
        position_bar, momentum_bar, grad_bar =\
            self.simulate_dynamics(self.model, position, momentum,
                                   stepsize_app, self.grad_log_pdf).get_proposed_values()

        acceptance_prob = self._acceptance_prob(position, position_bar, momentum, momentum_bar)

        # a = 2I[acceptance_prob] -1
        a = 2 * (acceptance_prob > 0.5) - 1

        condition = (acceptance_prob ** a) > (2 ** (-a))

        while condition:
            stepsize_app = (2 ** a) * stepsize_app

            position_bar, momentum_bar, grad_bar =\
                self.simulate_dynamics(self.model, position, momentum,
                                       stepsize_app, self.grad_log_pdf, grad_bar).get_proposed_values()

            acceptance_prob = self._acceptance_prob(position, position_bar, momentum, momentum_bar)

            condition = (acceptance_prob ** a) > (2 ** (-a))

        return stepsize_app

    def _sample(self, position, trajectory_length, stepsize, lsteps=None):
        """
        Runs a single sampling iteration to return a sample
        """
        # Resampling momentum
        momentum = np.reshape(np.random.normal(0, 1, len(position)), position.shape)

        # position_m here will be the previous sampled value of position
        position_bar, momentum_bar = position.copy(), momentum

        # Number of steps L to simulate dynamics
        if lsteps is None:
            lsteps = int(max(1, round(trajectory_length / stepsize, 0)))

        grad_bar, _ = self.model.get_gradient_log_pdf(position_bar, self.grad_log_pdf)

        for _ in range(lsteps):
            position_bar, momentum_bar, grad_bar =\
                self.simulate_dynamics(self.model, position_bar, momentum_bar,
                                       stepsize, self.grad_log_pdf, grad_bar).get_proposed_values()

        acceptance_prob = self._acceptance_prob(position, position_bar, momentum, momentum_bar)

        # Metropolis acceptance probability
        alpha = min(1, acceptance_prob)

        # Accept or reject the new proposed value of position, i.e position_bar
        if np.random.rand() < alpha:
            position = position_bar.copy()
            self.accepted_proposals += 1.0

        return position, alpha

    def sample(self, initial_pos, num_samples, trajectory_length, stepsize=None):
        """
        Method to return samples using Hamiltonian Monte Carlo

        Parameters
        ----------
        initial_pos: A 1d array like object
            Vector representing values of parameter position, the starting
            state in markov chain.

        num_samples: int
            Number of samples to be generated

        trajectory_length: int or float
            Target trajectory length, stepsize * number of steps(L),
            where L is the number of steps taken per HMC iteration,
            and stepsize is step size for splitting time method.

        stepsize: float , defaults to None
            The stepsize for proposing new values of position and momentum in simulate_dynamics
            If None, then will be choosen suitably


        Returns
        -------
        Returns two different types (based on installations)

        pandas.DataFrame: Returns samples as pandas.DataFrame if environment has a installation of pandas
        
        numpy.recarray: Returns samples in form of numpy recorded arrays (numpy.recarray)

        Examples
        --------
        >>> # Example if pandas is installed in working environment
        >>> from pgmpy.inference.continuous import HamiltonianMC as HMC, GradLogPDFGaussian, ModifiedEuler
        >>> from pgmpy.models import JointGaussianDistribution as JGD
        >>> import numpy as np
        >>> mean = np.array([1, -1])
        >>> covariance = np.array([[1, 0.2], [0.2, 1]])
        >>> model = JGD(['x', 'y'], mean, covariance)
        >>> sampler = HMC(model=model, grad_log_pdf=GradLogPDFGaussian, simulate_dynamics=ModifiedEuler)
        >>> samples = sampler.sample(np.array([1, 1]), num_samples = 5,
        ...                          trajectory_length=6, stepsize=0.25)
        >>> samples
                       x              y
        0  4.940656e-324  4.940656e-324
        1   1.592133e+00   1.152911e+00
        2   1.608700e+00   1.315349e+00
        3   1.608700e+00   1.315349e+00
        4   6.843856e-01   6.237043e-01
        >>> mean = np.array([4, 1, -1])
        >>> covariance = np.array([[1, 0.7 , 0.8], [0.7, 1, 0.2], [0.8, 0.2, 1]])
        >>> model = JGD(['x', 'y', 'z'], mean, covariance)
        >>> sampler = HMC(model=model, grad_log_pdf=GLPG)
        >>> samples = sampler.sample(np.array([1, 1]), num_samples = 10000,
        ...                          trajectory_length=6, stepsize=0.25)
        >>> np.cov(samples.values.T)
        array([[ 1.00795398,  0.71384233,  0.79802097],
               [ 0.71384233,  1.00633524,  0.21313767],
               [ 0.79802097,  0.21313767,  0.98519017]])
        """
        return_type = return_type.lower()
        if (return_type != 'dataframe' and return_type != 'recarray'):
            raise TypeError("return_type can be either 'dataframe' or 'recarray'")

        self.accepted_proposals = 1.0
        initial_pos = _check_1d_array_object(initial_pos, 'initial_pos')
        _check_length_equal(initial_pos, self.model.variables, 'initial_pos', 'model.variables')

        if stepsize is None:
            stepsize = self._find_reasonable_stepsize(initial_pos)

        types = [(var_name, 'float') for var_name in self.model.variables]
        samples = np.zeros(num_samples, dtype=types).view(np.recarray)
        samples[0] = initial_pos
        position_m = initial_pos

        lsteps = int(max(1, round(trajectory_length / stepsize, 0)))
        for i in range(1, num_samples):

            # Genrating sample
            position_m, _ = self._sample(position_m, trajectory_length, stepsize, lsteps)
            samples[i] = position_m

        self.acceptance_rate = self.accepted_proposals / num_samples

        if HAS_PANDAS == True:
            return pd.DataFrame.from_records(samples)

        return samples

    def generate_sample(self, initial_pos, num_samples, trajectory_length, stepsize=None):
        """
        Method returns a generator type object whose each iteration yields a sample
        using Hamiltonian Monte Carlo

        Parameters
        ----------
        initial_pos: A 1d array like object
            Vector representing values of parameter position, the starting
            state in markov chain.

        num_samples: int
            Number of samples to be generated

        trajectory_length: int or float
            Target trajectory length, stepsize * number of steps(L),
            where L is the number of steps taken per HMC iteration,
            and stepsize is step size for splitting time method.

        stepsize: float , defaults to None
            The stepsize for proposing new values of position and momentum in simulate_dynamics
            If None, then will be choosen suitably

        Returns
        -------
        genrator: yielding a numpy.array type object for a sample

        Examples
        --------
        >>> from pgmpy.inference.continuous import HamiltonianMC as HMC, GradLogPDFGaussian as GLPG
        >>> from pgmpy.models import JointGaussianDistribution as JGD
        >>> import numpy as np
        >>> mean = np.array([4, -1])
        >>> covariance = np.array([[3, 0.4], [0.4, 3]])
        >>> model = JGD(['x', 'y', 'z'], mean, covariance)
        >>> sampler = HMC(model=model, grad_log_pdf=GLPG)
        >>> gen_samples = sampler.generate_sample(np.array([-1, 1]), num_samples = 10,
        ...                                       trajectory_length=2, stepsize=None)
        >>> samples = [sample for sample in gen_samples]
        >>> samples_array = np.concatenate(samples, axis=1)
        >>> np.cov(samples_array)
        array([[ 1.00795398,  0.71384233,  0.79802097],
               [ 0.71384233,  1.00633524,  0.21313767],
               [ 0.79802097,  0.21313767,  0.98519017]])
        >>> sampler.acceptance_rate
        0.9978
        """

        self.accepted_proposals = 0
        initial_pos = _check_1d_array_object(initial_pos, 'initial_pos')
        _check_length_equal(initial_pos, self.model.variables, 'initial_pos', 'model.variables')

        if stepsize is None:
            stepsize = self._find_reasonable_stepsize(initial_pos)

        shape = (len(initial_pos), 1)
        lsteps = int(max(1, round(trajectory_length / stepsize, 0)))
        position_m = initial_pos.copy()

        for i in range(0, num_samples):

            position_m, _ = self._sample(position_m, trajectory_length, stepsize, lsteps)

            yield np.reshape(position_m, shape)

        self.acceptance_rate = self.accepted_proposals / num_samples


class HamiltonianMCda(HamiltonianMC):
    """
    Class for performing sampling in Continuous model
    using Hamiltonian Monte Carlo with dual averaging for
    adaptaion of parameter stepsize.

    Parameters:
    -----------
    model: An instance pgmpy.models
        Model from which sampling has to be done

    grad_log_pdf: A subclass of pgmpy.inference.continuous.GradientLogPDF
        Class to compute the log and gradient log of distribution

    simulate_dynamics: A subclass of pgmpy.inference.continuous.BaseSimulateHamiltonianDynamics
        Class to propose future states of position and momentum in time by simulating
        HamiltonianDynamics

    delta: float (in between 0 and 1), defaults to 0.65
        The target HMC acceptance probability

    Public Methods:
    ---------------
    sample()
    generate_sample()

    Example:
    --------
    >>> from pgmpy.inference.continuous import HamiltonianMCda as HMCda, LeapFrog
    >>> from pgmpy.models import JointGaussianDistribution as JGD
    >>> import numpy as np
    >>> mean = np.array([1, 2, 3])
    >>> covariance = np.array([[2, 0.4, 0.5], [0.4, 3, 0.6], [0.5, 0.6, 4]])
    >>> model = JGD(['x', 'y', 'z'], mean, covariance)
    >>> sampler = HMCda(model=model)
    >>> samples = sampler.sample(np.array([0, 0, 0]), num_adapt=10000, num_samples = 10000, trajectory_length=7)
    >>> samples_array = np.concatenate(samples, axis=1)
    >>> np.cov(samples_array)
    array([[ 1.83023816,  0.40449162,  0.51200707],
           [ 0.40449162,  2.85863596,  0.76747343],
           [ 0.51200707,  0.76747343,  3.87020982]])

    References
    -----------
    Matthew D. Hoffman, Andrew Gelman, The No-U-Turn Sampler: Adaptively
    Setting Path Lengths in Hamiltonian Monte Carlo. Journal of
    Machine Learning Research 15 (2014) 1351-1381
    Algorithm 5 : Hamiltonian Monte Carlo with dual averaging
    """

    def __init__(self, model, grad_log_pdf=None, simulate_dynamics=LeapFrog, delta=0.65):

        if not isinstance(delta, float) or delta > 1.0 or delta < 0.0:
            raise AttributeError(
                "delta should be a floating value in between 0 and 1")

        self.delta = delta

        super(HamiltonianMCda, self).__init__(model=model, grad_log_pdf=grad_log_pdf,
                                              simulate_dynamics=simulate_dynamics)

    def _adapt_params(self, stepsize, stepsize_bar, h_bar, mu, index_i, alpha):
        """
        Run tha adaptation for stepsize for better proposals of position
        """
        gamma = 0.05  # free parameter that controls the amount of shrinkage towards mu
        t0 = 10.0  # free parameter that stabilizes the initial iterations of the algorithm
        kappa = 0.75
        # See equation (6) section 3.2.1 for details

        estimate = 1.0 / (index_i + t0)
        h_bar = (1 - estimate) * h_bar + estimate * (self.delta - alpha)

        stepsize = np.exp(mu - sqrt(index_i) / gamma * h_bar)
        i_kappa = index_i ** (-kappa)
        stepsize_bar = np.exp(i_kappa * np.log(stepsize) + (1 - i_kappa) * np.log(stepsize_bar))

        return stepsize, stepsize_bar, h_bar

    def sample(self, initial_pos, num_adapt, num_samples, trajectory_length, stepsize=None):
        """
        Method to return samples using Hamiltonian Monte Carlo

        Parameters
        ----------
        initial_pos: A 1d array like object
            Vector representing values of parameter position, the starting
            state in markov chain.

        num_adapt: int
            The number of interations to run the adaptation of stepsize

        num_samples: int
            Number of samples to be generated

        trajectory_length: int or float
            Target trajectory length, stepsize * number of steps(L),
            where L is the number of steps taken per HMC iteration,
            and stepsize is step size for splitting time method.

        stepsize: float , defaults to None
            The stepsize for proposing new values of position and momentum in simulate_dynamics
            If None, then will be choosen suitably

        Returns
        -------
        Returns two different types (based on installations)

        pandas.DataFrame: Returns samples as pandas.DataFrame if environment has a installation of pandas
        
        numpy.recarray: Returns samples in form of numpy recorded arrays (numpy.recarray)

        Examples
        ---------
        >>> from pgmpy.inference.continuous import HamiltonianMCda as HMCda, GradLogPDFGaussian as GLPG, LeapFrog
        >>> from pgmpy.models import JointGaussianDistribution as JGD
        >>> import numpy as np
        >>> mean = np.array([1, 1])
        >>> covariance = np.array([[1, 0.7], [0.7, 3]])
        >>> model = JGD(['x', 'y'], mean, covariance)
        >>> sampler = HMCda(model=model, grad_log_pdf=GLPG, simulate_dynamics=LeapFrog)
        >>> samples = sampler.sample(np.array([1, 1]), num_adapt=10000,
        ...                          num_samples = 10000, trajectory_length=2, stepsize=None)
        >>> samples_array = np.concatenate(samples, axis=1)
        >>> np.cov(samples_array)
        array([[ 0.98432155,  0.66517394],
               [ 0.66517394,  2.95449533]])

        """

        self.accepted_proposals = 1.0

        initial_pos = _check_1d_array_object(initial_pos, 'initial_pos')
        _check_length_equal(initial_pos, self.model.variables, 'initial_pos', 'model.variables')

        if stepsize is None:
            stepsize = self._find_reasonable_stepsize(initial_pos)

        if num_adapt <= 1:  # Return samples genrated using Simple HMC algorithm
            return HamiltonianMC.sample(self, initial_pos, num_samples, stepsize, return_type=return_type)

        # stepsize is epsilon
        mu = np.log(10.0 * stepsize)  # freely chosen point, after each iteration xt(/position) is shrunk towards it
        # log(10 * stepsize) large values to save computation
        # stepsize_bar is epsilon_bar
        stepsize_bar = 1.0
        h_bar = 0.0
        # See equation (6) section 3.2.1 for details

        types = [(var_name, 'float') for var_name in self.model.variables]
        samples = np.zeros(num_samples, dtype=types).view(np.recarray)
        samples[0] = initial_pos
        position_m = initial_pos

        for i in range(1, num_samples):

            # Genrating sample
            position_m, alpha = self._sample(position_m, trajectory_length, stepsize)
            samples[i] = position_m

            # Adaptation of stepsize till num_adapt iterations
            if i <= num_adapt:
                stepsize, stepsize_bar, h_bar = self._adapt_params(stepsize, stepsize_bar, h_bar, mu, i, alpha)
            else:
                stepsize = stepsize_bar

        self.acceptance_rate = self.accepted_proposals / num_samples

        if HAS_PANDAS == True:
            return pd.DataFrame.from_records(samples)

        return samples

    def generate_sample(self, initial_pos, num_adapt, num_samples, trajectory_length, stepsize=None):
        """
        Method returns a generator type object whose each iteration yields a sample
        using Hamiltonian Monte Carlo

        Parameters
        ----------
        initial_pos: A 1d array like object
            Vector representing values of parameter position, the starting
            state in markov chain.

        num_adapt: int
            The number of interations to run the adaptation of stepsize

        num_samples: int
            Number of samples to be generated

        trajectory_length: int or float
            Target trajectory length, stepsize * number of steps(L),
            where L is the number of steps taken to propose new values of position and momentum
            per HMC iteration and stepsize is step size.

        stepsize: float , defaults to None
            The stepsize for proposing new values of position and momentum in simulate_dynamics
            If None, then will be choosen suitably

        Returns
        -------
        genrator: yielding a numpy.array type object for a sample

        Examples
        --------
        >>> from pgmpy.inference.continuous import HamiltonianMCda as HMCda, GradLogPDFGaussian as GLPG, LeapFrog
        >>> from pgmpy.models import JointGaussianDistribution as JGD
        >>> import numpy as np
        >>> mean = np.array([1, 1])
        >>> covariance = np.array([[1, 0.7], [0.7, 3]])
        >>> model = JGD(['x', 'y'], mean, covariance)
        >>> sampler = HMCda(model=model, grad_log_pdf=GLPG, simulate_dynamics=LeapFrog)
        >>> gen_samples = sampler.generate_sample(np.array([1, 1]), num_adapt=10000,
        ...                                       num_samples = 10000, trajectory_length=2, stepsize=None)
        >>> samples = [sample for sample in gen_samples]
        >>> samples_array = np.concatenate(samples, axis=1)
        >>> np.cov(samples_array)
        array([[ 0.98432155,  0.69517394],
               [ 0.69517394,  2.95449533]])
        """
        self.accepted_proposals = 0
        initial_pos = _check_1d_array_object(initial_pos, 'initial_pos')
        _check_length_equal(initial_pos, self.model.variables, 'initial_pos', 'model.variables')

        if stepsize is None:
            stepsize = self._find_reasonable_stepsize(initial_pos)

        if num_adapt <= 1:  # return sample generated using Simple HMC algorithm
            for sample in HamiltonianMC.generate_sample(self, initial_pos, num_samples, stepsize, trajectory_length):
                yield sample
            return
        mu = np.log(10.0 * stepsize)

        stepsize_bar = 1.0
        h_bar = 0.0

        position_m = initial_pos.copy()
        num_adapt += 1
        shape = (len(initial_pos), 1)
        for i in range(1, num_samples + 1):

            position_m, alpha = self._sample(position_m, trajectory_length, stepsize)

            if i <= num_adapt:
                stepsize, stepsize_bar, h_bar = self._adapt_params(stepsize, stepsize_bar, h_bar, mu, i, alpha)
            else:
                stepsize = stepsize_bar

            yield np.reshape(position_m, shape)

        self.acceptance_rate = self.accepted_proposals / num_samples
