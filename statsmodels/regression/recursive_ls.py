"""
Recursive least squares model

Author: Chad Fulton
License: Simplified-BSD
"""
from __future__ import division, absolute_import, print_function

try: unicode
except NameError: unicode = str

from warnings import warn
from statsmodels.compat.collections import OrderedDict

import numpy as np
import pandas as pd
from statsmodels.regression.linear_model import OLS
from statsmodels.tools.data import _is_using_pandas
from statsmodels.tsa.statespace.mlemodel import (
    MLEModel, MLEResults, MLEResultsWrapper, PredictionResults,
    PredictionResultsWrapper)
from statsmodels.tools.tools import Bunch
from statsmodels.tools.decorators import cache_readonly, resettable_cache
import statsmodels.base.wrapper as wrap

# Columns are alpha = 0.1, 0.05, 0.025, 0.01, 0.005
_cusum_squares_scalars = np.array([
    [1.0729830,   1.2238734,  1.3581015,  1.5174271,  1.6276236],
    [-0.6698868, -0.6700069, -0.6701218, -0.6702672, -0.6703724],
    [-0.5816458, -0.7351697, -0.8858694, -1.0847745, -1.2365861]
])


class RecursiveLS(MLEModel):
    r"""
    Recursive least squares

    Parameters
    ----------
    endog : array_like
        The observed time-series process :math:`y`
    exog : array_like
        Array of exogenous regressors, shaped nobs x k.
    constraints : array-like, str, or tuple
            - array : An r x k array where r is the number of restrictions to
              test and k is the number of regressors. It is assumed that the
              linear combination is equal to zero.
            - str : The full hypotheses to test can be given as a string.
              See the examples.
            - tuple : A tuple of arrays in the form (R, q), ``q`` can be
              either a scalar or a length p row vector.

    Notes
    -----
    Recursive least squares (RLS) corresponds to expanding window ordinary
    least squares (OLS).

    This model applies the Kalman filter to compute recursive estimates of the
    coefficients and recursive residuals.

    References
    ----------
    .. [*] Durbin, James, and Siem Jan Koopman. 2012.
       Time Series Analysis by State Space Methods: Second Edition.
       Oxford University Press.

    """
    def __init__(self, endog, exog, constraints=None, **kwargs):
        # Standardize data
        if not _is_using_pandas(endog, None):
            endog = np.asanyarray(endog)

        exog_is_using_pandas = _is_using_pandas(exog, None)
        if not exog_is_using_pandas:
            exog = np.asarray(exog)

        # Make sure we have 2-dimensional array
        if exog.ndim == 1:
            if not exog_is_using_pandas:
                exog = exog[:, None]
            else:
                exog = pd.DataFrame(exog)

        self.k_exog = exog.shape[1]

        # Handle constraints
        self.k_constraints = 0
        self._r_matrix = self._q_matrix = None
        if constraints is not None:
            from patsy import DesignInfo
            from statsmodels.base.data import handle_data
            data = handle_data(endog, exog, **kwargs)
            names = data.param_names
            LC = DesignInfo(names).linear_constraint(constraints)
            self._r_matrix, self._q_matrix = LC.coefs, LC.constants
            self.k_constraints = self._r_matrix.shape[0]

            endog = np.c_[endog, np.zeros((len(endog), len(self._r_matrix)))]
            endog[:, 1:] = self._q_matrix

        # Handle coefficient initialization
        kwargs.setdefault('initialization', 'diffuse')

        # Initialize the state space representation
        super(RecursiveLS, self).__init__(
            endog, k_states=self.k_exog, exog=exog, **kwargs)

        # Use univariate filtering by default
        self.ssm.filter_univariate = True

        # Concentrate the scale out of the likelihood function
        self.ssm.filter_concentrated = True

        # Setup the state space representation
        self['design'] = np.zeros((self.k_endog, self.k_states, self.nobs))
        self['design', 0] = self.exog[:, :, None].T
        if self._r_matrix is not None:
            self['design', 1:, :] = self._r_matrix[:, :, None]
        self['transition'] = np.eye(self.k_states)

        # Notice that the filter output does not depend on the measurement
        # variance, so we set it here to 1
        self['obs_cov', 0, 0] = 1.
        self['transition'] = np.eye(self.k_states)

        # Linear constraints are technically imposed by adding "fake" endog
        # variables that are used during filtering, but for all model- and
        # results-based purposes we want k_endog = 1.
        if self._r_matrix is not None:
            self.k_endog = 1

    @classmethod
    def from_formula(cls, formula, data, subset=None, constraints=None):
        return super(MLEModel, cls).from_formula(formula, data, subset,
                                                 constraints=constraints)

    def fit(self):
        """
        Fits the model by application of the Kalman filter

        Returns
        -------
        RecursiveLSResults
        """
        return self.smooth()

    def filter(self, return_ssm=False, **kwargs):
        # Get the state space output
        result = super(RecursiveLS, self).filter([], transformed=True,
                                                 cov_type='none',
                                                 return_ssm=True, **kwargs)

        # Wrap in a results object
        if not return_ssm:
            params = result.filtered_state[:, -1]
            cov_kwds = {
                'custom_cov_type': 'nonrobust',
                'custom_cov_params': result.filtered_state_cov[:, :, -1],
                'custom_description': ('Parameters and covariance matrix'
                                       ' estimates are RLS estimates'
                                       ' conditional on the entire sample.')
            }
            result = RecursiveLSResultsWrapper(
                RecursiveLSResults(self, params, result, cov_type='custom',
                                   cov_kwds=cov_kwds)
            )

        return result

    def smooth(self, return_ssm=False, **kwargs):
        # Get the state space output
        result = super(RecursiveLS, self).smooth([], transformed=True,
                                                 cov_type='none',
                                                 return_ssm=True, **kwargs)

        # Wrap in a results object
        if not return_ssm:
            params = result.filtered_state[:, -1]
            cov_kwds = {
                'custom_cov_type': 'nonrobust',
                'custom_cov_params': result.filtered_state_cov[:, :, -1],
                'custom_description': ('Parameters and covariance matrix'
                                       ' estimates are RLS estimates'
                                       ' conditional on the entire sample.')
            }
            result = RecursiveLSResultsWrapper(
                RecursiveLSResults(self, params, result, cov_type='custom',
                                   cov_kwds=cov_kwds)
            )

        return result

    @property
    def param_names(self):
        return self.exog_names

    @property
    def start_params(self):
        # Only parameter is the measurement disturbance standard deviation
        return np.zeros(0)

    def update(self, params, **kwargs):
        """
        Update the parameters of the model

        Updates the representation matrices to fill in the new parameter
        values.

        Parameters
        ----------
        params : array_like
            Array of new parameters.
        transformed : boolean, optional
            Whether or not `params` is already transformed. If set to False,
            `transform_params` is called. Default is True..

        Returns
        -------
        params : array_like
            Array of parameters.
        """
        pass


class RecursiveLSResults(MLEResults):
    """
    Class to hold results from fitting a recursive least squares model.

    Parameters
    ----------
    model : RecursiveLS instance
        The fitted model instance

    Attributes
    ----------
    specification : dictionary
        Dictionary including all attributes from the recursive least squares
        model instance.

    See Also
    --------
    statsmodels.tsa.statespace.kalman_filter.FilterResults
    statsmodels.tsa.statespace.mlemodel.MLEResults
    """

    def __init__(self, model, params, filter_results, cov_type='opg',
                 **kwargs):
        super(RecursiveLSResults, self).__init__(
            model, params, filter_results, cov_type, **kwargs)

        # Typically we don't use t distribution by default in state space
        # models, but we should here for comparison with typical OLS models
        self.use_t = True

        # Since we are overriding params with things that aren't MLE params,
        # need to adjust df's
        q = max(self.loglikelihood_burn, self.k_diffuse_states)
        self.df_model = q - self.model.k_constraints
        self.df_resid = self.nobs_effective - self.df_model

        # Save _init_kwds
        self._init_kwds = self.model._get_init_kwds()

        # Save the model specification
        self.specification = Bunch(**{
            'k_exog': self.model.k_exog,
            'k_constraints': self.model.k_constraints})

    def t_test(self, r_matrix, loc=None, cov_p=None, scale=None, use_t=None):
        """
        Compute a t-test for a each linear hypothesis of the form Rb = q

        Parameters
        ----------
        r_matrix : array-like, str, tuple
            - array : If an array is given, a p x k 2d array or length k 1d
              array specifying the linear restrictions. It is assumed
              that the linear combination is equal to zero.
            - str : The full hypotheses to test can be given as a string.
              See the examples.
            - tuple : A tuple of arrays in the form (R, q). If q is given,
              can be either a scalar or a length p row vector.
        loc : int, str, or datetime, optional
            Zero-indexed observation number at which to compute the t-test.
            Can also be a date string to parse or a datetime type. Default is
            to compute the t-test using the full-sample estimates. Cannot be
            used in combination with `cov_p`.
        cov_p : array-like
            An alternative estimate for the parameter covariance matrix.
            If None is given, self.normalized_cov_params is used.
        scale : float, optional
            An optional `scale` to use.  Default is the scale specified
            by the model fit.
        use_t : bool, optional
            If use_t is None, then the default of the model is used.
            If use_t is True, then the p-values are based on the t
            distribution.
            If use_t is False, then the p-values are based on the normal
            distribution.

        Returns
        -------
        res : ContrastResults instance
            The results for the test are attributes of this results instance.
            The available results have the same elements as the parameter table
            in `summary()`.

        Examples
        --------
        >>> import numpy as np
        >>> import statsmodels.api as sm
        >>> data = sm.datasets.longley.load()
        >>> data.exog = sm.add_constant(data.exog)
        >>> results = sm.OLS(data.endog, data.exog).fit()
        >>> r = np.zeros_like(results.params)
        >>> r[5:] = [1,-1]
        >>> print(r)
        [ 0.  0.  0.  0.  0.  1. -1.]

        r tests that the coefficients on the 5th and 6th independent
        variable are the same.

        >>> T_test = results.t_test(r)
        >>> print(T_test)
                                     Test for Constraints
        ==============================================================================
                         coef    std err          t      P>|t|      [0.025      0.975]
        ------------------------------------------------------------------------------
        c0         -1829.2026    455.391     -4.017      0.003   -2859.368    -799.037
        ==============================================================================
        >>> T_test.effect
        -1829.2025687192481
        >>> T_test.sd
        455.39079425193762
        >>> T_test.tvalue
        -4.0167754636411717
        >>> T_test.pvalue
        0.0015163772380899498

        Alternatively, you can specify the hypothesis tests using a string

        >>> from statsmodels.formula.api import ols
        >>> dta = sm.datasets.longley.load_pandas().data
        >>> formula = 'TOTEMP ~ GNPDEFL + GNP + UNEMP + ARMED + POP + YEAR'
        >>> results = ols(formula, dta).fit()
        >>> hypotheses = 'GNPDEFL = GNP, UNEMP = 2, YEAR/1829 = 1'
        >>> t_test = results.t_test(hypotheses)
        >>> print(t_test)
                                     Test for Constraints
        ==============================================================================
                         coef    std err          t      P>|t|      [0.025      0.975]
        ------------------------------------------------------------------------------
        c0            15.0977     84.937      0.178      0.863    -177.042     207.238
        c1            -2.0202      0.488     -8.231      0.000      -3.125      -0.915
        c2             1.0001      0.249      0.000      1.000       0.437       1.563
        ==============================================================================

        See Also
        ---------
        tvalues : individual t statistics
        f_test : for F tests
        patsy.DesignInfo.linear_constraint
        """
        from statsmodels.tools.tools import recipr
        from statsmodels.stats.contrast import ContrastResults
        from patsy import DesignInfo
        names = self.model.data.param_names
        LC = DesignInfo(names).linear_constraint(r_matrix)
        r_matrix, q_matrix = LC.coefs, LC.constants
        num_ttests = r_matrix.shape[0]
        num_params = r_matrix.shape[1]

        # Check we don't have conflicting `loc` and `cov_p`
        if loc is not None and cov_p is not None:
            raise ValueError('Cannot use `loc` in combination with `cov_p`.')

        if num_params != self.params.shape[0]:
            raise ValueError('r_matrix and params are not aligned')
        if q_matrix is None:
            q_matrix = np.zeros(num_ttests)
        else:
            q_matrix = np.asarray(q_matrix)
            q_matrix = q_matrix.squeeze()
        if q_matrix.size > 1:
            if q_matrix.shape[0] != num_ttests:
                raise ValueError("r_matrix and q_matrix must have the same "
                                 "number of rows")

        if use_t is None:
            # switch to use_t false if undefined
            use_t = (hasattr(self, 'use_t') and self.use_t)

        _t = _sd = None

        # If we've been given a location
        if loc is not None:
            t, _, out_of_sample, _ = (
                self.model._get_prediction_index(loc, loc, index=None))
            if t > self.model.nobs:
                raise ValueError('Given t-test location is not in the sample.')
            # Get the appropriate cov
            params = self.filtered_state[:, t]
            cov_p = self.filtered_state_cov[:, :, t]

            # Rescale the covariance parameters based on the scale estiamted
            # as of observation t
            d = max(self.loglikelihood_burn, self.nobs_diffuse)
            nmissing = np.sum(self.filter_results.nmissing[d:t + 1])
            n = (t + 1 - d) - nmissing
            scale = (np.sum(self.filter_results.scale_obs[d:t + 1]) / n)
            cov_p *= scale / self.scale
        else:
            d = max(self.loglikelihood_burn, self.nobs_diffuse)
            t = self.model.nobs
            nmissing = np.sum(self.filter_results.nmissing[d:])
            params = self.params

        _effect = np.dot(r_matrix, params)
        # nan_dot multiplies with the convention nan * 0 = 0

        if num_ttests > 1:
            _sd = np.sqrt(np.diag(self.cov_params(
                r_matrix=r_matrix, cov_p=cov_p)))
        else:
            _sd = np.sqrt(self.cov_params(r_matrix=r_matrix, cov_p=cov_p))
        _t = (_effect - q_matrix) * recipr(_sd)

        nobs_effective = (t + 1) - nmissing - self.loglikelihood_burn
        df_resid = nobs_effective - self.df_model

        if use_t:
            return ContrastResults(effect=_effect, t=_t, sd=_sd,
                                   df_denom=df_resid)
        else:
            return ContrastResults(effect=_effect, statistic=_t, sd=_sd,
                                   df_denom=df_resid,
                                   distribution='norm')

    @property
    def recursive_coefficients(self):
        """
        Estimates of regression coefficients, recursively estimated

        Returns
        -------
        out: Bunch
            Has the following attributes:

            - `filtered`: a time series array with the filtered estimate of
                          the component
            - `filtered_cov`: a time series array with the filtered estimate of
                          the variance/covariance of the component
            - `smoothed`: a time series array with the smoothed estimate of
                          the component
            - `smoothed_cov`: a time series array with the smoothed estimate of
                          the variance/covariance of the component
            - `offset`: an integer giving the offset in the state vector where
                        this component begins
        """
        out = None
        spec = self.specification
        start = offset = 0
        end = offset + spec.k_exog
        out = Bunch(
            filtered=self.filtered_state[start:end],
            filtered_cov=self.filtered_state_cov[start:end, start:end],
            smoothed=None, smoothed_cov=None,
            offset=offset
        )
        if self.smoothed_state is not None:
            out.smoothed = self.smoothed_state[start:end]
        if self.smoothed_state_cov is not None:
            out.smoothed_cov = (
                self.smoothed_state_cov[start:end, start:end])
        return out

    @cache_readonly
    def resid_recursive(self):
        r"""
        Recursive residuals

        Returns
        -------
        resid_recursive : array_like
            An array of length `nobs` holding the recursive
            residuals.

        Notes
        -----
        These quantities are defined in, for example, Harvey (1989)
        section 5.4. In fact, there he defines the standardized innovations in
        equation 5.4.1, but in his version they have non-unit variance, whereas
        the standardized forecast errors computed by the Kalman filter here
        assume unit variance. To convert to Harvey's definition, we need to
        multiply by the standard deviation.

        Harvey notes that in smaller samples, "although the second moment
        of the :math:`\tilde \sigma_*^{-1} \tilde v_t`'s is unity, the
        variance is not necessarily equal to unity as the mean need not be
        equal to zero", and he defines an alternative version (which are
        not provided here).

        """
        return (self.filter_results.standardized_forecasts_error[0] *
                self.scale**0.5)

    @cache_readonly
    def cusum(self):
        r"""
        Cumulative sum of standardized recursive residuals statistics

        Returns
        -------
        cusum : array_like
            An array of length `nobs - k_exog` holding the
            CUSUM statistics.


        Notes
        -----
        The CUSUM statistic takes the form:

        .. math::

            W_t = \frac{1}{\hat \sigma} \sum_{j=k+1}^t w_j

        where :math:`w_j` is the recursive residual at time :math:`j` and
        :math:`\hat \sigma` is the estimate of the standard deviation
        from the full sample.

        Excludes the first `k_exog` datapoints.

        Due to differences in the way :math:`\hat \sigma` is calculated, the
        output of this function differs slightly from the output in the
        R package strucchange and the Stata contributed .ado file cusum6. The
        calculation in this package is consistent with the description of
        Brown et al. (1975)

        References
        ----------
        .. [*] Brown, R. L., J. Durbin, and J. M. Evans. 1975.
           "Techniques for Testing the Constancy of
           Regression Relationships over Time."
           Journal of the Royal Statistical Society.
           Series B (Methodological) 37 (2): 149-92.

        """
        d = max(self.nobs_diffuse, self.loglikelihood_burn)
        return (np.cumsum(self.resid_recursive[d:]) /
                np.std(self.resid_recursive[d:], ddof=1))

    @cache_readonly
    def cusum_squares(self):
        r"""
        Cumulative sum of squares of standardized recursive residuals
        statistics

        Returns
        -------
        cusum_squares : array_like
            An array of length `nobs - k_exog` holding the
            CUSUM of squares statistics.

        Notes
        -----
        The CUSUM of squares statistic takes the form:

        .. math::

            s_t = \left ( \sum_{j=k+1}^t w_j^2 \right ) \Bigg /
                  \left ( \sum_{j=k+1}^T w_j^2 \right )

        where :math:`w_j` is the recursive residual at time :math:`j`.

        Excludes the first `k_exog` datapoints.

        References
        ----------
        .. [*] Brown, R. L., J. Durbin, and J. M. Evans. 1975.
           "Techniques for Testing the Constancy of
           Regression Relationships over Time."
           Journal of the Royal Statistical Society.
           Series B (Methodological) 37 (2): 149-92.

        """
        d = max(self.nobs_diffuse, self.loglikelihood_burn)
        numer = np.cumsum(self.resid_recursive[d:]**2)
        denom = numer[-1]
        return numer / denom

    @cache_readonly
    def llf_recursive_obs(self):
        """
        (float) Loglikelihood at observation, computed from recursive residuals
        """
        from scipy.stats import norm
        return np.log(norm.pdf(self.resid_recursive, loc=0,
                               scale=self.scale**0.5))

    @cache_readonly
    def llf_recursive(self):
        """
        (float) Loglikelihood defined by recursive residuals, equivalent to OLS
        """
        return np.sum(self.llf_recursive_obs)

    @cache_readonly
    def ssr(self):
        d = max(self.nobs_diffuse, self.loglikelihood_burn)
        return (self.nobs - d) * self.filter_results.obs_cov[0, 0, 0]

    @cache_readonly
    def centered_tss(self):
        return np.sum((self.filter_results.endog[0] -
                       np.mean(self.filter_results.endog))**2)

    @cache_readonly
    def uncentered_tss(self):
        return np.sum((self.filter_results.endog[0])**2)

    @cache_readonly
    def ess(self):
        if self.k_constant:
            return self.centered_tss - self.ssr
        else:
            return self.uncentered_tss - self.ssr

    @cache_readonly
    def rsquared(self):
        if self.k_constant:
            return 1 - self.ssr / self.centered_tss
        else:
            return 1 - self.ssr / self.uncentered_tss

    @cache_readonly
    def mse_model(self):
        return self.ess / self.df_model

    @cache_readonly
    def mse_resid(self):
        return self.ssr / self.df_resid

    @cache_readonly
    def mse_total(self):
        if self.k_constant:
            return self.centered_tss / (self.df_resid + self.df_model)
        else:
            return self.uncentered_tss / (self.df_resid + self.df_model)

    def get_prediction(self, start=None, end=None, dynamic=False,
                       index=None, **kwargs):
        # Note: need to override this, because we currently don't support
        # dynamic prediction or forecasts when there are constraints.
        if start is None:
            start = self.model._index[0]

        # Handle start, end, dynamic
        start, end, out_of_sample, prediction_index = (
            self.model._get_prediction_index(start, end, index))

        # Handle `dynamic`
        if isinstance(dynamic, (bytes, unicode)):
            dynamic, _, _ = self.model._get_index_loc(dynamic)

        if self.model._r_matrix is not None and (out_of_sample or dynamic):
            raise NotImplementedError('Cannot yet perform out-of-sample or'
                                      ' dynamic prediction in models with'
                                      ' constraints.')

        # Perform the prediction
        # This is a (k_endog x npredictions) array; don't want to squeeze in
        # case of npredictions = 1
        prediction_results = self.filter_results.predict(
            start, end + out_of_sample + 1, dynamic, **kwargs)

        # Return a new mlemodel.PredictionResults object
        return PredictionResultsWrapper(PredictionResults(
            self, prediction_results, row_labels=prediction_index))
    get_prediction.__doc__ = MLEResults.get_prediction.__doc__

    def plot_recursive_coefficient(self, variables=0, alpha=0.05,
                                   legend_loc='upper left', fig=None,
                                   figsize=None):
        r"""
        Plot the recursively estimated coefficients on a given variable

        Parameters
        ----------
        variables : int or str or iterable of int or string, optional
            Integer index or string name of the variable whose coefficient will
            be plotted. Can also be an iterable of integers or strings. Default
            is the first variable.
        alpha : float, optional
            The confidence intervals for the coefficient are (1 - alpha) %
        legend_loc : string, optional
            The location of the legend in the plot. Default is upper left.
        fig : Matplotlib Figure instance, optional
            If given, subplots are created in this figure instead of in a new
            figure. Note that the grid will be created in the provided
            figure using `fig.add_subplot()`.
        figsize : tuple, optional
            If a figure is created, this argument allows specifying a size.
            The tuple is (width, height).

        Notes
        -----
        All plots contain (1 - `alpha`) %  confidence intervals.
        """
        # Get variables
        if isinstance(variables, (int, str)):
            variables = [variables]
        k_variables = len(variables)

        # If a string was given for `variable`, try to get it from exog names
        exog_names = self.model.exog_names
        for i in range(k_variables):
            variable = variables[i]
            if isinstance(variable, str):
                variables[i] = exog_names.index(variable)

        # Create the plot
        from scipy.stats import norm
        from statsmodels.graphics.utils import _import_mpl, create_mpl_fig
        plt = _import_mpl()
        fig = create_mpl_fig(fig, figsize)

        for i in range(k_variables):
            variable = variables[i]
            ax = fig.add_subplot(k_variables, 1, i + 1)

            # Get dates, if applicable
            if hasattr(self.data, 'dates') and self.data.dates is not None:
                dates = self.data.dates._mpl_repr()
            else:
                dates = np.arange(self.nobs)
            d = max(self.nobs_diffuse, self.loglikelihood_burn)

            # Plot the coefficient
            coef = self.recursive_coefficients
            ax.plot(dates[d:], coef.filtered[variable, d:],
                    label='Recursive estimates: %s' % exog_names[variable])

            # Legend
            handles, labels = ax.get_legend_handles_labels()

            # Get the critical value for confidence intervals
            if alpha is not None:
                critical_value = norm.ppf(1 - alpha / 2.)

                # Plot confidence intervals
                std_errors = np.sqrt(coef.filtered_cov[variable, variable, :])
                ci_lower = (
                    coef.filtered[variable] - critical_value * std_errors)
                ci_upper = (
                    coef.filtered[variable] + critical_value * std_errors)
                ci_poly = ax.fill_between(
                    dates[d:], ci_lower[d:], ci_upper[d:], alpha=0.2
                )
                ci_label = ('$%.3g \\%%$ confidence interval'
                            % ((1 - alpha)*100))

                # Only add CI to legend for the first plot
                if i == 0:
                    # Proxy artist for fill_between legend entry
                    # See http://matplotlib.org/1.3.1/users/legend_guide.html
                    p = plt.Rectangle((0, 0), 1, 1,
                                      fc=ci_poly.get_facecolor()[0])

                    handles.append(p)
                    labels.append(ci_label)

            ax.legend(handles, labels, loc=legend_loc)

            # Remove xticks for all but the last plot
            if i < k_variables - 1:
                ax.xaxis.set_ticklabels([])

        fig.tight_layout()

        return fig

    def _cusum_significance_bounds(self, alpha, ddof=0, points=None):
        """
        Parameters
        ----------
        alpha : float, optional
            The significance bound is alpha %.
        ddof : int, optional
            The number of periods additional to `k_exog` to exclude in
            constructing the bounds. Default is zero. This is usually used
            only for testing purposes.
        points : iterable, optional
            The points at which to evaluate the significance bounds. Default is
            two points, beginning and end of the sample.

        Notes
        -----
        Comparing against the cusum6 package for Stata, this does not produce
        exactly the same confidence bands (which are produced in cusum6 by
        lw, uw) because they burn the first k_exog + 1 periods instead of the
        first k_exog. If this change is performed
        (so that `tmp = (self.nobs - d - 1)**0.5`), then the output here
        matches cusum6.

        The cusum6 behavior does not seem to be consistent with
        Brown et al. (1975); it is likely they did that because they needed
        three initial observations to get the initial OLS estimates, whereas
        we do not need to do that.
        """
        # Get the constant associated with the significance level
        if alpha == 0.01:
            scalar = 1.143
        elif alpha == 0.05:
            scalar = 0.948
        elif alpha == 0.10:
            scalar = 0.950
        else:
            raise ValueError('Invalid significance level.')

        # Get the points for the significance bound lines
        d = max(self.nobs_diffuse, self.loglikelihood_burn)
        tmp = (self.nobs - d - ddof)**0.5
        upper_line = lambda x: scalar * tmp + 2 * scalar * (x - d) / tmp

        if points is None:
            points = np.array([d, self.nobs])
        return -upper_line(points), upper_line(points)

    def plot_cusum(self, alpha=0.05, legend_loc='upper left',
                   fig=None, figsize=None):
        r"""
        Plot the CUSUM statistic and significance bounds.

        Parameters
        ----------
        alpha : float, optional
            The plotted significance bounds are alpha %.
        legend_loc : string, optional
            The location of the legend in the plot. Default is upper left.
        fig : Matplotlib Figure instance, optional
            If given, subplots are created in this figure instead of in a new
            figure. Note that the grid will be created in the provided
            figure using `fig.add_subplot()`.
        figsize : tuple, optional
            If a figure is created, this argument allows specifying a size.
            The tuple is (width, height).

        Notes
        -----
        Evidence of parameter instability may be found if the CUSUM statistic
        moves out of the significance bounds.

        References
        ----------
        .. [*] Brown, R. L., J. Durbin, and J. M. Evans. 1975.
           "Techniques for Testing the Constancy of
           Regression Relationships over Time."
           Journal of the Royal Statistical Society.
           Series B (Methodological) 37 (2): 149-92.

        """
        # Create the plot
        from statsmodels.graphics.utils import _import_mpl, create_mpl_fig
        plt = _import_mpl()
        fig = create_mpl_fig(fig, figsize)
        ax = fig.add_subplot(1, 1, 1)

        # Get dates, if applicable
        if hasattr(self.data, 'dates') and self.data.dates is not None:
            dates = self.data.dates._mpl_repr()
        else:
            dates = np.arange(self.nobs)
        d = max(self.nobs_diffuse, self.loglikelihood_burn)

        # Plot cusum series and reference line
        ax.plot(dates[d:], self.cusum, label='CUSUM')
        ax.hlines(0, dates[d], dates[-1], color='k', alpha=0.3)

        # Plot significance bounds
        lower_line, upper_line = self._cusum_significance_bounds(alpha)
        ax.plot([dates[d], dates[-1]], upper_line, 'k--',
                label='%d%% significance' % (alpha * 100))
        ax.plot([dates[d], dates[-1]], lower_line, 'k--')

        ax.legend(loc=legend_loc)

        return fig

    def _cusum_squares_significance_bounds(self, alpha, points=None):
        """
        Notes
        -----
        Comparing against the cusum6 package for Stata, this does not produce
        exactly the same confidence bands (which are produced in cusum6 by
        lww, uww) because they use a different method for computing the
        critical value; in particular, they use tabled values from
        Table C, pp. 364-365 of "The Econometric Analysis of Time Series"
        Harvey, (1990), and use the value given to 99 observations for any
        larger number of observations. In contrast, we use the approximating
        critical values suggested in Edgerton and Wells (1994) which allows
        computing relatively good approximations for any number of
        observations.
        """
        # Get the approximate critical value associated with the significance
        # level
        d = max(self.nobs_diffuse, self.loglikelihood_burn)
        n = 0.5 * (self.nobs - d) - 1
        try:
            ix = [0.1, 0.05, 0.025, 0.01, 0.005].index(alpha / 2)
        except ValueError:
            raise ValueError('Invalid significance level.')
        scalars = _cusum_squares_scalars[:, ix]
        crit = scalars[0] / n**0.5 + scalars[1] / n + scalars[2] / n**1.5

        # Get the points for the significance bound lines
        if points is None:
            points = np.array([d, self.nobs])
        line = (points - d) / (self.nobs - d)

        return line - crit, line + crit

    def plot_cusum_squares(self, alpha=0.05, legend_loc='upper left',
                           fig=None, figsize=None):
        r"""
        Plot the CUSUM of squares statistic and significance bounds.

        Parameters
        ----------
        alpha : float, optional
            The plotted significance bounds are alpha %.
        legend_loc : string, optional
            The location of the legend in the plot. Default is upper left.
        fig : Matplotlib Figure instance, optional
            If given, subplots are created in this figure instead of in a new
            figure. Note that the grid will be created in the provided
            figure using `fig.add_subplot()`.
        figsize : tuple, optional
            If a figure is created, this argument allows specifying a size.
            The tuple is (width, height).

        Notes
        -----
        Evidence of parameter instability may be found if the CUSUM of squares
        statistic moves out of the significance bounds.

        Critical values used in creating the significance bounds are computed
        using the approximate formula of [1]_.

        References
        ----------
        .. [*] Brown, R. L., J. Durbin, and J. M. Evans. 1975.
           "Techniques for Testing the Constancy of
           Regression Relationships over Time."
           Journal of the Royal Statistical Society.
           Series B (Methodological) 37 (2): 149-92.
        .. [1] Edgerton, David, and Curt Wells. 1994.
           "Critical Values for the Cusumsq Statistic
           in Medium and Large Sized Samples."
           Oxford Bulletin of Economics and Statistics 56 (3): 355-65.

        """
        # Create the plot
        from statsmodels.graphics.utils import _import_mpl, create_mpl_fig
        plt = _import_mpl()
        fig = create_mpl_fig(fig, figsize)
        ax = fig.add_subplot(1, 1, 1)

        # Get dates, if applicable
        if hasattr(self.data, 'dates') and self.data.dates is not None:
            dates = self.data.dates._mpl_repr()
        else:
            dates = np.arange(self.nobs)
        d = max(self.nobs_diffuse, self.loglikelihood_burn)

        # Plot cusum series and reference line
        ax.plot(dates[d:], self.cusum_squares, label='CUSUM of squares')
        ref_line = (np.arange(d, self.nobs) - d) / (self.nobs - d)
        ax.plot(dates[d:], ref_line, 'k', alpha=0.3)

        # Plot significance bounds
        lower_line, upper_line = self._cusum_squares_significance_bounds(alpha)
        ax.plot([dates[d], dates[-1]], upper_line, 'k--',
                label='%d%% significance' % (alpha * 100))
        ax.plot([dates[d], dates[-1]], lower_line, 'k--')

        ax.legend(loc=legend_loc)

        return fig


class RecursiveLSResultsWrapper(MLEResultsWrapper):
    _attrs = {}
    _wrap_attrs = wrap.union_dicts(MLEResultsWrapper._wrap_attrs,
                                   _attrs)
    _methods = {}
    _wrap_methods = wrap.union_dicts(MLEResultsWrapper._wrap_methods,
                                     _methods)
wrap.populate_wrapper(RecursiveLSResultsWrapper, RecursiveLSResults)
