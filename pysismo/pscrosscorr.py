#!/usr/bin/env python
"""
Module that contains classes holding cross-correlations and related
processing, such as frequency-time analysis (FTAN) to measure
dispersion curves.
"""

import pserrors, psstation, psutils, pstomo
import obspy.signal
import obspy.io.xseed
import obspy.signal.cross_correlation
import obspy.signal.filter
from obspy.core import AttribDict, read, UTCDateTime, Trace
from obspy.signal.invsim import cosine_taper
import numpy as np
from numpy.fft import rfft, irfft, fft, ifft, fftfreq
from scipy import integrate
from scipy.interpolate import RectBivariateSpline, interp1d
from scipy.optimize import minimize
import itertools as it
import os
import shutil
import glob
import pickle
import copy
from collections import OrderedDict
import datetime as dt
import math

import matplotlib.pyplot as plt
import matplotlib as mpl
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib import gridspec

from matplotlib.colors import ColorConverter
from matplotlib.colors import LinearSegmentedColormap

plt.ioff()  # turning off interactive mode

# ====================================================
# parsing configuration file to import some parameters
# ====================================================
from psconfig import (
    CROSSCORR_DIR, FTAN_DIR, PERIOD_BANDS, CROSSCORR_WINDOW, CROSSCORR_SHIFT,
    PERIOD_RESAMPLE, CROSSCORR_SKIPLOCS, MINFILL, FREQMIN, FREQMAX, CORNERS, ZEROPHASE,
    ONEBIT_NORM, FREQMIN_EARTHQUAKE, FREQMAX_EARTHQUAKE, WINDOW_TIME, WINDOW_FREQ,
    SIGNAL_WINDOW_VMIN, SIGNAL_WINDOW_VMAX, SIGNAL2NOISE_TRAIL, NOISE_WINDOW_SIZE,
    RAWFTAN_PERIODS, CLEANFTAN_PERIODS, FTAN_VELOCITIES, FTAN_ALPHA, STRENGTH_SMOOTHING,
    USE_INSTANTANEOUS_FREQ, MAX_RELDIFF_INST_NOMINAL_PERIOD, MIN_INST_PERIOD,
    HALFWINDOW_MEDIAN_PERIOD, MAX_RELDIFF_INST_MEDIAN_PERIOD, BBOX_LARGE, BBOX_SMALL,
    CONTROL_PERIOD_LENGTH)

# ========================
# Constants and parameters
# ========================

EPS = 1.0e-5
ONESEC = dt.timedelta(seconds=1)

# custom color map for seismic anomalies
# --------------------------------------
c = ColorConverter()
#colors = ['black', 'red', 'gold', 'white',
#          'white', 'aquamarine', 'blue', 'magenta']

#values = [-1.0, -0.35, -0.1, -0.025,
#          0.025, 0.1, 0.95, 1.0]
          
colors = ['darkblue', 'blue', 'lightblue', 'aquamarine',
          'green', 'yellow', 'red', 'darkred']
values = [-1, 0, 0.55, 0.6,
          0.7, 0.8, 0.95, 1.0]
rgblist = [c.to_rgb(s) for s in colors]
reds, greens, blues = zip(*rgblist)
cdict = {}
for x, r, g, b in zip(values, reds, greens, blues):
    v = (x - min(values)) / (max(values) - min(values))
    cdict.setdefault('red', []).append((v, r, r))
    cdict.setdefault('green', []).append((v, g, g))
    cdict.setdefault('blue', []).append((v, b, b))
CMAP_SEISMIC = LinearSegmentedColormap('customseismic', cdict)


class MonthYear:
    """
    Hashable class holding a month of a year
    """
    def __init__(self, *args, **kwargs):
        """
        Usage: MonthYear(3, 2012) or MonthYear(month=3, year=2012) or
               MonthYear(date[time](2012, 3, 12))
        """
        if len(args) == 2 and not kwargs:
            month, year = args
        elif not args and set(kwargs.keys()) == {'month', 'year'}:
            month, year = kwargs['month'], kwargs['year']
        elif len(args) == 1 and not kwargs:
            month, year = args[0].month, args[0].year
        else:
            s = ("Usage: MonthYear(3, 2012) or MonthYear(month=3, year=2012) or "
                 "MonthYear(date[time](2012, 3, 12))")
            raise Exception(s)

        self.m = month
        self.y = year

    def __str__(self):
        """
        E.g., 03-2012
        """
        return '{:02d}-{}'.format(self.m, self.y)

    def __repr__(self):
        """
        E.g., <03-2012>
        """
        return '<{}>'.format(str(self))

    def __eq__(self, other):
        """
        Comparison with other, which can be a MonthYear object,
        or a sequence of int (month, year)
        @type other: L{MonthYear} or (int, int)
        """
        try:
            return self.m == other.m and self.y == other.y
        except:
            try:
                return (self.m, self.y) == tuple(other)
            except:
                return False

    def __hash__(self):
        return hash(self.m) ^ hash(self.y)


class ControlCrossCorrelation:
    """
    Class holding cross-correlation over a subset control period
    """
    def __init__(self, control_period, control_period_length, ndata):
        """
        @type control_period: int
        @type control_period_length: int
        @type ndata: int
        """
        # attaching control period and control period length (seconds)
        self.control_period = control_period
        self.control_period_length = control_period_length

        # initializing stats
        self.nslice = 0

        # data array of month cross-correlation
        self.dataarray = np.zeros(ndata)

    # def monthfill(self):
    #     """
    #     Returns the relative month fill (between 0-1)
    #     """
    #     return float(self.nslice) / monthrange(year=self.month.y, month=self.month.m)[1]

    def __repr__(self):
        daylength = int(self.control_period_length / (60 * 60 * 24))
        s = '<cross-correlation over single {} day control period {}: {} timeslices>'
        return s.format(daylength, self.control_period, self.nslice)


class CrossCorrelation:
    """
    Cross-correlation class, which contains:
    - a pair of stations
    - a pair of sets of locations (from trace.location)
    - a pair of sets of ids (from trace.id)
    - start time, end time and nb of timeslices of cross-correlation
    - distance between stations
    - distance between stations
    - a time array and a (cross-correlation) data array
    """

    def __init__(self, station1, station2, xcorr_dt=PERIOD_RESAMPLE,
                 xcorr_tmax=CROSSCORR_SHIFT):
        """
        @type station1: L{pysismo.psstation.Station}
        @type station2: L{pysismo.psstation.Station}
        @type xcorr_dt: float
        @type xcorr_tmax: float
        """
        # pair of stations
        self.station1 = station1
        self.station2 = station2

        # locations and trace ids of stations
        self.locs1 = set()
        self.locs2 = set()
        self.ids1 = set()
        self.ids2 = set()

        # initializing stats
        self.starttime = None
        self.endtime = None
        self.nslice = 0

        # initializing time and data arrays of cross-correlation
        nmax = int(xcorr_tmax / xcorr_dt)
        self.timearray = np.arange(-nmax * xcorr_dt, (nmax + 1)*xcorr_dt, xcorr_dt)
        self.dataarray = np.zeros(2 * nmax + 1)

        #  has cross-corr been symmetrized? whitened?
        self.symmetrized = False
        self.whitened = False

        # initializing list of cross-correlations over a single month
        self.controlxcs = []

    def __repr__(self):
        s = '<cross-correlation between stations {0}-{1}: avg {2} timeslices>'
        return s.format(self.station1.name, self.station2.name, self.nslice)

    def __str__(self):
        """
        E.g., 'Cross-correlation between stations SPB['10'] - ITAB['00','10']:
               365 timeslices from 2002-01-01 to 2002-12-01'
        """
        locs1 = ','.join(sorted("'{}'".format(loc) for loc in self.locs1))
        locs2 = ','.join(sorted("'{}'".format(loc) for loc in self.locs2))
        s = ('Cross-correlation between stations '
             '{sta1}[{locs1}]-{sta2}[{locs2}]: '
             '{nslice} timeslices from {start} to {end}')
        return s.format(sta1=self.station1.name, locs1=locs1,
                        sta2=self.station2.name, locs2=locs2, nslice=self.nslice,
                        start=self.starttime, end=self.endtime)

    def dist(self):
        """
        Geodesic distance (in km) between stations, using the
        WGS-84 ellipsoidal model of the Earth
        """
        return self.station1.dist(self.station2)

    def copy(self):
        """
        Makes a copy of self
        """
        # shallow copy
        result = copy.copy(self)
        # copy of cross-correlations over control period
        result.controlxcs = [copy.copy(mxc) for mxc in self.controlxcs]
        return result

    def add(self, tr1, tr2, xcorr=None):
        """
        Stacks cross-correlation between 2 traces
        @type tr1: L{obspy.core.trace.Trace}
        @type tr2: L{obspy.core.trace.Trace}
        """
        # verifying sampling rates
        try:
            assert abs(1.0 / tr1.stats.sampling_rate - self._get_xcorr_dt()) < EPS
            assert abs(1.0 / tr2.stats.sampling_rate - self._get_xcorr_dt()) < EPS
        except AssertionError:
            s = 'Sampling rates of traces are not equal to that of cross-corr ({r:.1f} Hz):\n{tr1}\n{tr2}'
            raise Exception(s.format(r=1.0 / self._get_xcorr_dt(), tr1=tr1, tr2=tr2))

        # cross-correlation
        if xcorr is None:
            # calculating cross-corr using obspy, if not already provided
            xcorr = obspy.signal.cross_correlation.correlate(
                tr1, tr2, shift=self._get_xcorr_nmax(), demean=False, normalize=None)

        # verifying that we don't have NaN
        if np.any(np.isnan(xcorr)):
            s = u"Got NaN in cross-correlation between traces:\n{tr1}\n{tr2}"
            raise pserrors.NaNError(s.format(tr1=tr1, tr2=tr2))

        # stacking cross-corr
        self.dataarray += xcorr
        # updating stats: 1st timeslice, last timeslice, nb of timeslices of cross-corr
        starttime= (tr1.stats.starttime + ONESEC)
        self.starttime = min(self.starttime, starttime) if self.starttime else starttime
        endtime = (tr1.stats.endtime - ONESEC)
        self.endtime = max(self.endtime, endtime) if self.endtime else endtime
        self.nslice += 1

        # stacking cross-corr over single control period of length CONTROL_PERIOD_LENGTH
        control_period_length = CONTROL_PERIOD_LENGTH  * 24 * 60 * 60
        control_period = int((tr1.stats.starttime + ONESEC - self.starttime) / control_period_length)

        try:
            controlxc = next(controlxc for controlxc in self.controlxcs
                           if controlxc.control_period == control_period)
        except StopIteration:
            # appending new month xc
            controlxc = ControlCrossCorrelation(control_period=control_period,
                                                control_period_length=control_period_length,
                                                ndata=len(self.timearray))
            self.controlxcs.append(controlxc)
        controlxc.dataarray += xcorr
        controlxc.nslice += 1

        # updating (adding) locs and ids
        self.locs1.add(tr1.stats.location)
        self.locs2.add(tr2.stats.location)
        self.ids1.add(tr1.id)
        self.ids2.add(tr2.id)

    def symmetrize(self, inplace=False):
        """
        Symmetric component of cross-correlation (including
        the list of cross-corr over a single month).
        Returns self if already symmetrized or inPlace=True

        @rtype: CrossCorrelation
        """

        if self.symmetrized:
            # already symmetrized
            return self

        # symmetrizing on self or copy of self
        xcout = self if inplace else self.copy()

        n = len(xcout.timearray)
        mid = (n - 1) / 2

        # verifying that time array is symmetric wrt 0
        if n % 2 != 1:
            raise Exception('Cross-correlation cannot be symmetrized')
        if not np.alltrue(xcout.timearray[mid:] + xcout.timearray[mid::-1] < EPS):
            raise Exception('Cross-correlation cannot be symmetrized')

        # calculating symmetric component of cross-correlation
        xcout.timearray = xcout.timearray[mid:]
        for obj in [xcout] + (xcout.controlxcs if hasattr(xcout, 'controlxcs') else []):
            a = obj.dataarray
            obj.dataarray = (a[mid:] + a[mid::-1]) / 2.0

        xcout.symmetrized = True
        return xcout

    def whiten(self, inplace=False, window_freq=0.004,
               bandpass_tmin=7.0, bandpass_tmax=150):
        """
        Spectral whitening of cross-correlation (including
        the list of cross-corr over a single month).
        @rtype: CrossCorrelation
        """
        if hasattr(self, 'whitened') and self.whitened:
            # already whitened
            return self

        # whitening on self or copy of self
        xcout = self if inplace else self.copy()

        # frequency step
        npts = len(xcout.timearray)
        sampling_rate = 1.0 / xcout._get_xcorr_dt()
        deltaf = sampling_rate / npts

        # loop over cross-corr and one-month stacks
        for obj in [xcout] + (xcout.controlxcs if hasattr(xcout, 'controlxcs') else []):
            a = obj.dataarray
            # Fourier transform
            ffta = rfft(a)

            # smoothing amplitude spectrum
            halfwindow = int(round(window_freq / deltaf / 2.0))
            weight = psutils.moving_avg(abs(ffta), halfwindow=halfwindow)
            a[:] = irfft(ffta / weight, n=npts)

            # bandpass to avoid low/high freq noise
            obj.dataarray = psutils.bandpass_butterworth(data=a,
                                                         dt=xcout._get_xcorr_dt(),
                                                         periodmin=bandpass_tmin,
                                                         periodmax=bandpass_tmax)

        xcout.whitened = True
        return xcout

    def signal_noise_windows(self, vmin, vmax, signal2noise_trail, noise_window_size):
        """
        Returns the signal window and the noise window.
        The signal window is defined by *vmin* and *vmax*:

          dist/*vmax* < t < dist/*vmin*

        The noise window starts *signal2noise_trail* after the
        signal window and has a size of *noise_window_size*:

          t > dist/*vmin* + *signal2noise_trail*
          t < dist/*vmin* + *signal2noise_trail* + *noise_window_size*

        If the noise window hits the time limit of the cross-correlation,
        we try to extend it to the left until it hits the signal
        window.

        @rtype: (float, float), (float, float)
        """
        # signal window
        tmin_signal = self.dist() / vmax
        tmax_signal = self.dist() / vmin

        # noise window
        tmin_noise = tmax_signal + signal2noise_trail
        tmax_noise = tmin_noise + noise_window_size
        if tmax_noise > self.timearray.max():
            # the noise window hits the rightmost limit:
            # let's shift it to the left without crossing
            # the signal window
            delta = min(tmax_noise - self.timearray.max(), tmin_noise - tmax_signal)
            tmin_noise -= delta
            tmax_noise -= delta

        return (tmin_signal, tmax_signal), (tmin_noise, tmax_noise)

    def SNR(self, periodbands=None,
            centerperiods_and_alpha=None,
            whiten=False, control_periods=None,
            vmin=SIGNAL_WINDOW_VMIN,
            vmax=SIGNAL_WINDOW_VMAX,
            signal2noise_trail=SIGNAL2NOISE_TRAIL,
            noise_window_size=NOISE_WINDOW_SIZE):
        """
        [spectral] signal-to-noise ratio, calculated as the peak
        of the absolute amplitude in the signal window divided by
        the standard deviation in the noise window.

        If period bands are given (in *periodbands*, as a list of
        (periodmin, periodmax)), then for each band the SNR is
        calculated after band-passing the cross-correlation using
        a butterworth filter.

        If center periods and alpha are given (in *centerperiods_and_alpha*,
        as a list of (center period, alpha)), then for each center
        period and alpha the SNR is calculated after band-passing
        the cross-correlation using a Gaussian filter

        The signal window is defined by *vmin* and *vmax*:

          dist/*vmax* < t < dist/*vmin*

        The noise window starts *signal2noise_trail* after the
        signal window and has a size of *noise_window_size*:

          t > dist/*vmin* + *signal2noise_trail*
          t < dist/*vmin* + *signal2noise_trail* + *noise_window_size*

        If the noise window hits the time limit of the cross-correlation,
        we try to extend it to the left until it hits the signal
        window.

        @type periodbands: (list of (float, float))
        @type whiten: bool
        @type vmin: float
        @type vmax: float
        @type signal2noise_trail: float
        @type noise_window_size: float
        @type months: list of (L{MonthYear} or (int, int))
        @rtype: L{numpy.ndarray}
        """
        # symmetric part of cross-corr
        xcout = self.symmetrize(inplace=False)

        # spectral whitening
        if whiten:
            xcout = xcout.whiten(inplace=False)

        # cross-corr of desired months
        xcdata = xcout._get_controlperiod_xcdataarray(control_periods=control_periods)

        # filter type and associated arguments
        if periodbands:
            filtertype = 'Butterworth'
            kwargslist = [{'periodmin': band[0], 'periodmax': band[1]}
                          for band in periodbands]
        elif centerperiods_and_alpha:
            filtertype = 'Gaussian'
            kwargslist = [{'period': period, 'alpha': alpha}
                          for period, alpha in centerperiods_and_alpha]
        else:
            filtertype = None
            kwargslist = [{}]

        SNR = []
        for filterkwargs in kwargslist:
            if not filtertype:
                dataarray = xcdata
            else:
                # bandpass filtering data before calculating SNR
                dataarray = psutils.bandpass(data=xcdata,
                                             dt=xcout._get_xcorr_dt(),
                                             filtertype=filtertype,
                                             **filterkwargs)

            # signal and noise windows
            tsignal, tnoise = xcout.signal_noise_windows(
                vmin, vmax, signal2noise_trail, noise_window_size)

            signal_window = (xcout.timearray >= tsignal[0]) & \
                            (xcout.timearray <= tsignal[1])

            noise_window = (xcout.timearray >= tnoise[0]) & \
                           (xcout.timearray <= tnoise[1])

            peak = np.abs(dataarray[signal_window]).max()
            noise = dataarray[noise_window].std()

            # appending SNR
            SNR.append(peak / noise)

        # returning 1d array if spectral SNR, 0d array if normal SNR
        return np.array(SNR) if len(SNR) > 1 else np.array(SNR[0])

    def plot(self, whiten=False, sym=False, signal_window_position='both', vmin=SIGNAL_WINDOW_VMIN,
             vmax=SIGNAL_WINDOW_VMAX, control_periods=None, xlim_xcorr=None, 
             xlim_spectrum=None, figsize=None):
        """
        Plots cross-correlation and its spectrum
        """
        xcout = self.symmetrize(inplace=False) if sym else self
        if whiten:
            xcout = xcout.whiten(inplace=False)

        # cross-corr of desired months
        xcdata = xcout._get_controlperiod_xcdataarray(control_periods=control_periods)

        # cross-correlation plot ===
        plt.figure(figsize=figsize)
        plt.subplot(2, 1, 1)
        plt.plot(xcout.timearray, xcdata)
        plt.xlabel('Time (s)')
        plt.ylabel('Cross-correlation')

        # vmin, vmax
        def signal_window(vmin, vmax):
            vkwargs = {
                'fontsize': 8,
                'horizontalalignment': 'center',
                'verticalalignment': 'bottom',
                'bbox': {'edgecolor': 'black', 'facecolor': 'white', 'alpha': 0.5}}
            ylim = plt.ylim()
            
            if vmin and vmax:
                plt.plot(2 * [xcout.dist() / vmin], ylim, color='grey')
                xy = (xcout.dist() / vmin, ylim[0])
                plt.annotate('{0} km/s'.format(vmin), xy=xy, xytext=xy, **vkwargs)

                plt.plot(2 * [xcout.dist() / vmax], ylim, color='grey')
                xy = (xcout.dist() / vmax, ylim[0])
                plt.annotate('{0} km/s'.format(vmax), xy=xy, xytext=xy, **vkwargs)
                
                plt.fill_between((xcout.dist() / vmin, xcout.dist() / vmax), ylim[0], ylim[1], color='lightgray')
            
            plt.ylim(ylim)

        if signal_window_position == 'both':
            signal_window(vmin, vmax)
            signal_window(-vmin, -vmax)
        elif signal_window_position == 'right':
            signal_window(vmin, vmax)
        elif signal_window_position == 'left':
            signal_window(-vmin, -vmax)
            
        plt.xlim(xlim_xcorr)
        plt.grid()

        # title
        plt.title(xcout._plottitle(control_period=control_periods))

        # spectrum plot ===
        plt.subplot(2, 1, 2)
        plt.xlabel('Frequency (Hz)')
        plt.ylabel('Amplitude')
        plt.grid()

        # frequency and amplitude arrays
        npts = len(xcdata)
        nfreq = npts / 2 + 1 if npts % 2 == 0 else (npts + 1) / 2
        sampling_rate = 1.0 / xcout._get_xcorr_dt()
        freqarray = np.arange(nfreq) * sampling_rate / npts
        amplarray = np.abs(rfft(xcdata))
        plt.plot(freqarray, amplarray)
        
        if xlim_spectrum:
            plt.xlim(xlim_spectrum)
        else:
            plt.xlim((min(freqarray), max(freqarray)))
        plt.tight_layout()
        plt.show()

    def plot_by_period_band(self, axlist=None, bands=PERIOD_BANDS,
                            plot_title=True, whiten=False, tmax=None,
                            vmin=SIGNAL_WINDOW_VMIN,
                            vmax=SIGNAL_WINDOW_VMAX,
                            signal2noise_trail=SIGNAL2NOISE_TRAIL,
                            noise_window_size=NOISE_WINDOW_SIZE,
                            control_periods=None, outfile=None):
        """
        Plots cross-correlation for various bands of periods

        The signal window:
            vmax / dist < t < vmin / dist,
        and the noise window:
            t > vmin / dist + signal2noise_trail
            t < vmin / dist + signal2noise_trail + noise_window_size,
        serve to estimate the SNRs and are highlighted on the plot.

        If *tmax* is not given, default is to show times up to the noise
        window (plus 5 %). The y-scale is adapted to fit the min and max
        cross-correlation AFTER the beginning of the signal window.

        @type axlist: list of L{matplotlib.axes.AxesSubplot}
        """
        # one plot per band + plot of original xcorr
        nplot = len(bands) + 1

        # limits of time axis
        if not tmax:
            # default is to show time up to the noise window (plus 5 %)
            tmax = self.dist() / vmin + signal2noise_trail + noise_window_size
            tmax = min(1.05 * tmax, self.timearray.max())
        xlim = (0, tmax)

        # creating figure if not given as input
        fig = None
        if not axlist:
            fig = plt.figure()
            axlist = [fig.add_subplot(nplot, 1, i) for i in range(1, nplot + 1)]

        for ax in axlist:
            # smaller y tick label
            ax.tick_params(axis='y', labelsize=9)

        axlist[0].get_figure().subplots_adjust(hspace=0)

        # symmetrization
        xcout = self.symmetrize(inplace=False)

        # spectral whitening
        if whiten:
            xcout = xcout.whiten(inplace=False)

        # cross-corr of desired months
        xcdata = xcout._get_controlperiod_xcdataarray(control_periods=control_periods)

        # limits of y-axis = min/max of the cross-correlation
        # AFTER the beginning of the signal window
        mask = (xcout.timearray >= min(self.dist() / vmax, xlim[1])) & \
               (xcout.timearray <= xlim[1])
        ylim = (xcdata[mask].min(), xcdata[mask].max())

        # signal and noise windows
        tsignal, tnoise = xcout.signal_noise_windows(
            vmin, vmax, signal2noise_trail, noise_window_size)

        # plotting original cross-correlation
        axlist[0].plot(xcout.timearray, xcdata)

        # title
        if plot_title:
            title = xcout._plottitle(prefix='Cross-corr. ', control_period=control_periods)
            axlist[0].set_title(title)

        # signal window
        for t, v, align in zip(tsignal, [vmax, vmin], ['right', 'left']):
            axlist[0].plot(2 * [t], ylim, color='k', lw=1.5)
            xy = (t, ylim[0] + 0.1 * (ylim[1] - ylim[0]))
            axlist[0].annotate(s='{} km/s'.format(v), xy=xy, xytext=xy,
                               horizontalalignment=align, fontsize=8,
                               bbox={'edgecolor': 'black', 'facecolor': 'white', 'alpha': 0.5})

        # noise window
        axlist[0].fill_between(x=tnoise, y1=[ylim[1], ylim[1]],
                               y2=[ylim[0], ylim[0]], color='k', alpha=0.2)

        # inserting text, e.g., "Original data, SNR = 10.1"
        SNR = xcout.SNR(vmin=vmin, vmax=vmax,
                        signal2noise_trail=signal2noise_trail,
                        noise_window_size=noise_window_size)
        axlist[0].text(x=xlim[1],
                       y=ylim[0] + 0.85 * (ylim[1] - ylim[0]),
                       s="Original data, SNR = {:.1f}".format(float(SNR)),
                       fontsize=9,
                       horizontalalignment='right',
                       bbox={'edgecolor': 'black', 'facecolor': 'white', 'alpha': 0.5})

        # formatting axes
        axlist[0].set_xlim(xlim)
        axlist[0].set_ylim(ylim)
        axlist[0].grid(True)
        # formatting labels
        axlist[0].set_xticklabels([])
        axlist[0].get_figure().canvas.draw()
        labels = [l.get_text() for l in axlist[0].get_yticklabels()]
        labels[0] = labels[-1] = ''
        labels[2:-2] = [''] * (len(labels) - 4)
        axlist[0].set_yticklabels(labels)

        # plotting band-filtered cross-correlation
        for ax, (tmin, tmax) in zip(axlist[1:], bands):
            lastplot = ax is axlist[-1]

            dataarray = psutils.bandpass_butterworth(data=xcdata,
                                                     dt=xcout._get_xcorr_dt(),
                                                     periodmin=tmin,
                                                     periodmax=tmax)
            # limits of y-axis = min/max of the cross-correlation
            # AFTER the beginning of the signal window
            mask = (xcout.timearray >= min(self.dist() / vmax, xlim[1])) & \
                   (xcout.timearray <= xlim[1])
            ylim = (dataarray[mask].min(), dataarray[mask].max())

            ax.plot(xcout.timearray, dataarray)

            # signal window
            for t in tsignal:
                ax.plot(2 * [t], ylim, color='k', lw=2)

            # noise window
            ax.fill_between(x=tnoise, y1=[ylim[1], ylim[1]],
                            y2=[ylim[0], ylim[0]], color='k', alpha=0.2)

            # inserting text, e.g., "10 - 20 s, SNR = 10.1"
            SNR = float(xcout.SNR(periodbands=[(tmin, tmax)],
                                  vmin=vmin, vmax=vmax,
                                  signal2noise_trail=signal2noise_trail,
                                  noise_window_size=noise_window_size))
            ax.text(x=xlim[1],
                    y=ylim[0] + 0.85 * (ylim[1] - ylim[0]),
                    s="{} - {} s, SNR = {:.1f}".format(tmin, tmax, SNR),
                    fontsize=9,
                    horizontalalignment='right',
                    bbox={'edgecolor': 'black', 'facecolor': 'white', 'alpha': 0.5})

            if lastplot:
                # adding label to signalwindows
                ax.text(x=self.dist() * (1.0 / vmin + 1.0 / vmax) / 2.0,
                        y=ylim[0] + 0.1 * (ylim[1] - ylim[0]),
                        s="Signal window",
                        horizontalalignment='center',
                        fontsize=8,
                        bbox={'edgecolor': 'black', 'facecolor': 'white', 'alpha': 0.5})

                # adding label to noise windows
                ax.text(x=sum(tnoise) / 2,
                        y=ylim[0] + 0.1 * (ylim[1] - ylim[0]),
                        s="Noise window",
                        horizontalalignment='center',
                        fontsize=8,
                        bbox={'edgecolor': 'black', 'facecolor': 'white', 'alpha': 0.5})

            # formatting axes
            ax.set_xlim(xlim)
            ax.set_ylim(ylim)
            ax.grid(True)
            if lastplot:
                ax.set_xlabel('Time (s)')
            # formatting labels
            if not lastplot:
                ax.set_xticklabels([])
            ax.get_figure().canvas.draw()
            labels = [l.get_text() for l in ax.get_yticklabels()]
            labels[0] = labels[-1] = ''
            labels[2:-2] = [''] * (len(labels) - 4)
            ax.set_yticklabels(labels)

        if outfile:
            axlist[0].gcf().savefig(outfile, dpi=300, transparent=True)

        if fig:
            fig.show()

    def FTAN(self, whiten=False, phase_corr=None, control_periods=None, vgarray_init=None,
             optimize_curve=None, strength_smoothing=STRENGTH_SMOOTHING,
             use_inst_freq=USE_INSTANTANEOUS_FREQ, vg_at_nominal_freq=None,
             debug=False):
        """
        Frequency-time analysis of a cross-correlation function.

        Calculates the Fourier transform of the cross-correlation,
        calculates the analytic signal in the frequency domain,
        applies Gaussian bandpass filters centered around given
        center periods, calculates the filtered analytic
        signal back in time domain and extracts the group velocity
        dispersion curve.

        Options:
        - set *whiten*=True to whiten the spectrum of the cross-corr.
        - provide a function of frequency in *phase_corr* to include a
          phase correction.
        - provide a list of (int) in *control_periods* to restrict the FTAN
          to a subset of control periods
        - provide an initial guess of dispersion curve (in *vgarray_init*)
          to accelerate the group velocity curve extraction
        - set *optimize_curve*=True to further optimize the dispersion
          curve, i.e., find the curve that really minimizes the penalty
          function (which seeks to maximize the traversed amplitude while
          penalizing jumps) -- but not necessarily rides through
          local maxima any more. Default is True for the raw FTAN (no phase
          corr provided), False for the clean FTAN (phase corr provided)
        - set the strength of the smoothing term of the dispersion curve
          in *strength_smoothing*
        - set *use_inst_freq*=True to replace the nominal frequency with
          the instantaneous frequency in the dispersion curve.
        - if an array is provided in *vg_at_nominal_freq*, then it is filled
          with the vg curve BEFORE the nominal freqs are replaced with
          instantaneous freqs

        Returns (1) the amplitude matrix A(T0,v), (2) the phase matrix
        phi(T0,v) (that is, the amplitude and phase function of velocity
        v of the analytic signal filtered around period T0) and (3) the
        group velocity disperion curve extracted from the amplitude
        matrix.

        Raises CannotCalculateInstFreq if the calculation of instantaneous
        frequencies only gives bad values.

        FTAN periods in variable *RAWFTAN_PERIODS* and *CLEANFTAN_PERIODS*
        FTAN velocities in variable *FTAN_VELOCITIES*

        See. e.g., Levshin & Ritzwoller, "Automated detection,
        extraction, and measurement of regional surface waves",
        Pure Appl. Geoph. (2001) and Bensen et al., "Processing
        seismic ambient noise data to obtain reliable broad-band
        surface wave dispersion measurements", Geophys. J. Int. (2007).

        @type whiten: bool
        @type phase_corr: L{scipy.interpolate.interpolate.interp1d}
        @type months: list of (L{MonthYear} or (int, int))
        @type vgarray_init: L{numpy.ndarray}
        @type vg_at_nominal_freq: L{numpy.ndarray}
        @rtype: (L{numpy.ndarray}, L{numpy.ndarray}, L{DispersionCurve})
        """
        # no phase correction given <=> raw FTAN
        raw_ftan = phase_corr is None
        if optimize_curve is None:
            optimize_curve = raw_ftan
        ftan_periods = RAWFTAN_PERIODS if raw_ftan else CLEANFTAN_PERIODS

        # getting the symmetrized cross-correlation
        xcout = self.symmetrize(inplace=False)
        # whitening cross-correlation
        if whiten:
            xcout = xcout.whiten(inplace=False)

        # cross-corr of desired control periods
        xcdata = xcout._get_controlperiod_xcdataarray(control_periods=control_periods)
        if xcdata is None:
            raise Exception('No data to perform FTAN in selected control periods')

        # FTAN analysis: amplitute and phase function of
        # center periods T0 and time t
        ampl, phase = FTAN(x=xcdata,
                           dt=xcout._get_xcorr_dt(),
                           periods=ftan_periods,
                           alpha=FTAN_ALPHA,
                           phase_corr=phase_corr)

        # re-interpolating amplitude and phase as functions
        # of center periods T0 and velocities v
        tne0 = xcout.timearray != 0.0
        x = ftan_periods                                 # x = periods
        y = (self.dist() / xcout.timearray[tne0])[::-1]  # y = velocities
        zampl = ampl[:, tne0][:, ::-1]                   # z = amplitudes
        zphase = phase[:, tne0][:, ::-1]                 # z = phases
        # spline interpolation
        ampl_interp_func = RectBivariateSpline(x, y, zampl)
        phase_interp_func = RectBivariateSpline(x, y, zphase)
        # re-sampling over periods and velocities
        ampl_resampled = ampl_interp_func(ftan_periods, FTAN_VELOCITIES)
        phase_resampled = phase_interp_func(ftan_periods, FTAN_VELOCITIES)

        # extracting the group velocity curve from the amplitude matrix,
        # that is, the velocity curve that maximizes amplitude and best
        # avoids jumps
        vgarray = extract_dispcurve(amplmatrix=ampl_resampled,
                                    velocities=FTAN_VELOCITIES,
                                    varray_init=vgarray_init,
                                    optimizecurve=optimize_curve,
                                    strength_smoothing=strength_smoothing)
        if not vg_at_nominal_freq is None:
            # filling array with group velocities before replacing
            # nominal freqs with instantaneous freqs
            vg_at_nominal_freq[...] = vgarray

        # if *use_inst_freq*=True, we replace nominal freq with instantaneous
        # freq, i.e., we consider that ampl[iT, :], phase[iT, :] and vgarray[iT]
        # actually correspond to period 2.pi/|dphi/dt|(t=arrival time), with
        # phi(.) = phase[iT, :]  and arrival time = dist / vgarray[iT],
        # and we re-interpolate them along periods of *ftan_periods*

        nom2inst_periods = None
        if use_inst_freq:
            # array of arrival times
            tarray = xcout.dist() / vgarray
            # indices of arrival times in time array
            it = xcout.timearray.searchsorted(tarray)
            it = np.minimum(len(xcout.timearray) - 1, np.maximum(1, it))
            # instantaneous freq: omega = |dphi/dt|(t=arrival time),
            # with phi = phase of FTAN
            dt = xcout.timearray[it] - xcout.timearray[it-1]
            nT = phase.shape[0]
            omega = np.abs((phase[range(nT), it] - phase[range(nT), it-1]) / dt)
            # -> instantaneous period = 2.pi/omega
            inst_periods = 2.0 * np.pi / omega
            assert isinstance(inst_periods, np.ndarray)  # just to enable autocompletion

            if debug:
                plt.plot(ftan_periods, inst_periods)

            # removing outliers (inst periods too small or too different from nominal)
            reldiffs = np.abs((inst_periods - ftan_periods) / ftan_periods)
            discard = (inst_periods < MIN_INST_PERIOD) | \
                      (reldiffs > MAX_RELDIFF_INST_NOMINAL_PERIOD)
            inst_periods = np.where(discard, np.nan, inst_periods)
            # despiking curve of inst freqs (by removing values too
            # different from the running median)
            n = np.size(inst_periods)
            median_periods = []
            for i in range(n):
                sl = slice(max(i - HALFWINDOW_MEDIAN_PERIOD, 0),
                           min(i + HALFWINDOW_MEDIAN_PERIOD + 1, n))
                mask = ~np.isnan(inst_periods[sl])
                if np.any(mask):
                    med = np.median(inst_periods[sl][mask])
                    median_periods.append(med)
                else:
                    median_periods.append(np.nan)
            reldiffs = np.abs((inst_periods - np.array(median_periods)) / inst_periods)
            mask = ~np.isnan(reldiffs)
            inst_periods[mask] = np.where(reldiffs[mask] > MAX_RELDIFF_INST_MEDIAN_PERIOD,
                                          np.nan,
                                          inst_periods[mask])

            # filling holes by linear interpolation
            masknan = np.isnan(inst_periods)
            if masknan.all():
                # not a single correct value of inst period!
                s = "Not a single correct value of instantaneous period!"
                raise pserrors.CannotCalculateInstFreq(s)
            if masknan.any():
                inst_periods[masknan] = np.interp(x=masknan.nonzero()[0],
                                                  xp=(~masknan).nonzero()[0],
                                                  fp=inst_periods[~masknan])

            # looking for the increasing curve that best-fits
            # calculated instantaneous periods
            def fun(periods):
                # misfit wrt calculated instantaneous periods
                return np.sum((periods - inst_periods)**2)
            # constraints = positive increments
            constraints = [{'type': 'ineq', 'fun': lambda p, i=i: p[i+1] - p[i]}
                           for i in range(len(inst_periods) - 1)]

            res = minimize(fun, x0=ftan_periods, method='SLSQP', constraints=constraints)
            inst_periods = res['x']

            if debug:
                plt.plot(ftan_periods, inst_periods)
                plt.show()

            # re-interpolating amplitude, phase and dispersion curve
            # along periods of array *ftan_periods* -- assuming that
            # their are currently evaluated along *inst_periods*
            vgarray = np.interp(x=ftan_periods,
                                xp=inst_periods,
                                fp=vgarray,
                                left=np.nan,
                                right=np.nan)
            for iv in range(len(FTAN_VELOCITIES)):
                ampl_resampled[:, iv] = np.interp(x=ftan_periods,
                                                  xp=inst_periods,
                                                  fp=ampl_resampled[:, iv],
                                                  left=np.nan,
                                                  right=np.nan)
                phase_resampled[:, iv] = np.interp(x=ftan_periods,
                                                   xp=inst_periods,
                                                   fp=phase_resampled[:, iv],
                                                   left=np.nan,
                                                   right=np.nan)

            # list of (nominal period, inst period)
            nom2inst_periods = zip(ftan_periods, inst_periods)

        vgcurve = pstomo.DispersionCurve(periods=ftan_periods,
                                         v=vgarray,
                                         station1=self.station1,
                                         station2=self.station2,
                                         nom2inst_periods=nom2inst_periods)

        return ampl_resampled, phase_resampled, vgcurve

    def FTAN_complete(self, whiten=False, control_periods=None, add_SNRs=True,
                      vmin=SIGNAL_WINDOW_VMIN, vmax=SIGNAL_WINDOW_VMAX,
                      signal2noise_trail=SIGNAL2NOISE_TRAIL,
                      noise_window_size=NOISE_WINDOW_SIZE,
                      optimize_curve=None,
                      strength_smoothing=STRENGTH_SMOOTHING,
                      use_inst_freq=USE_INSTANTANEOUS_FREQ,
                      **kwargs):
        """
        Frequency-time analysis including phase-matched filter and
        seasonal variability:

        (1) Performs a FTAN of the raw cross-correlation signal,
        (2) Uses the raw group velocities to calculate the phase corr.
        (3) Performs a FTAN with the phase correction
            ("phase matched filter")
        (4) Repeats the procedure for all control periods if no
            list of control periods is given

        Optionally, adds spectral SNRs at the periods of the clean
        vg curve. In this case, parameters *vmin*, *vmax*,
        *signal2noise_trail*, *noise_window_size* control the location
        of the signal window and the noise window
        (see function xc.SNR()).

        Options:
        - set *whiten*=True to whiten the spectrum of the cross-corr.
        - provide a list of (int, int) in *months* to restrict the FTAN
          to a subset of month-year
        - set *add_SNRs* to calculate the SNR function of period associated
          with the disperions curves
        - adjust the signal window and the noise window of the SNR through
          *vmin*, *vmax*, *signal2noise_trail*, *noise_window_size*
        - set *optimize_curve*=True to further optimize the dispersion
          curve, i.e., find the curve that really minimizes the penalty
          function (which seeks to maximize the traversed amplitude while
          preserving smoothness) -- but not necessarily rides through
          local maxima. Default is True for the raw FTAN, False for the
          clean FTAN
        - set the strength of the smoothing term of the dispersion curve
          in *strength_smoothing*
        - other *kwargs* sent to CrossCorrelation.FTAN()

        Returns raw ampl, raw vg, cleaned ampl, cleaned vg.

        See. e.g., Levshin & Ritzwoller, "Automated detection,
        extraction, and measurement of regional surface waves",
        Pure Appl. Geoph. (2001) and Bensen et al., "Processing
        seismic ambient noise data to obtain reliable broad-band
        surface wave dispersion measurements", Geophys. J. Int. (2007).

        @type whiten: bool
        @type months: list of (L{MonthYear} or (int, int))
        @type add_SNRs: bool
        @rtype: (L{numpy.ndarray}, L{numpy.ndarray},
                 L{numpy.ndarray}, L{DispersionCurve})
        """
        # symmetrized, whitened cross-corr
        xc = self.symmetrize(inplace=False)
        if whiten:
            xc = xc.whiten(inplace=False)

        # raw FTAN (no need to whiten any more)
        rawvg_init = np.zeros_like(RAWFTAN_PERIODS)
        try:
            rawampl, _, rawvg = xc.FTAN(whiten=False,
                                        control_periods=control_periods,
                                        optimize_curve=optimize_curve,
                                        strength_smoothing=strength_smoothing,
                                        use_inst_freq=use_inst_freq,
                                        vg_at_nominal_freq=rawvg_init,
                                        **kwargs)
        except pserrors.CannotCalculateInstFreq:
            # pb with instantaneous frequency: returnin NaNs
            print "Warning: could not calculate instantenous frequencies in raw FTAN!"
            rawampl = np.nan * np.zeros((len(RAWFTAN_PERIODS), len(FTAN_VELOCITIES)))
            cleanampl = np.nan * np.zeros((len(CLEANFTAN_PERIODS), len(FTAN_VELOCITIES)))
            rawvg = pstomo.DispersionCurve(periods=RAWFTAN_PERIODS,
                                           v=np.nan * np.zeros(len(RAWFTAN_PERIODS)),
                                           station1=self.station1,
                                           station2=self.station2)
            cleanvg = pstomo.DispersionCurve(periods=CLEANFTAN_PERIODS,
                                             v=np.nan * np.zeros(len(CLEANFTAN_PERIODS)),
                                             station1=self.station1,
                                             station2=self.station2)
            return rawampl, rawvg, cleanampl, cleanvg

        # phase function from raw vg curve
        phase_corr = xc.phase_func(vgcurve=rawvg)

        # clean FTAN
        cleanvg_init = np.zeros_like(CLEANFTAN_PERIODS)
        try:
            cleanampl, _, cleanvg = xc.FTAN(whiten=False,
                                            phase_corr=phase_corr,
                                            control_periods=control_periods,
                                            optimize_curve=optimize_curve,
                                            strength_smoothing=strength_smoothing,
                                            use_inst_freq=use_inst_freq,
                                            vg_at_nominal_freq=cleanvg_init,
                                            **kwargs)
        except pserrors.CannotCalculateInstFreq:
            # pb with instantaneous frequency: returnin NaNs
            print "Warning: could not calculate instantenous frequencies in clean FTAN!"
            cleanampl = np.nan * np.zeros((len(CLEANFTAN_PERIODS), len(FTAN_VELOCITIES)))
            cleanvg = pstomo.DispersionCurve(periods=CLEANFTAN_PERIODS,
                                             v=np.nan * np.zeros(len(CLEANFTAN_PERIODS)),
                                             station1=self.station1,
                                             station2=self.station2)
            return rawampl, rawvg, cleanampl, cleanvg

        # adding spectral SNRs associated with the periods of the
        # clean vg curve
        if add_SNRs:
            cleanvg.add_SNRs(xc, control_periods=control_periods,
                             vmin=vmin, vmax=vmax,
                             signal2noise_trail=signal2noise_trail,
                             noise_window_size=noise_window_size)

        if control_periods is None:
            # set of available months (without year)
            available_control_periods = set(cxc.control_period for cxc in xc.controlxcs)

            # extracting clean vg curves for all 12 trimesters:
            # Jan-Feb-March, Feb-March-Apr ... Dec-Jan-Feb
            for control_period in available_control_periods:
            # for trimester_start in range(1, 13):
            #     # months of trimester, e.g. [1, 2, 3], [2, 3, 4] ... [12, 1, 2]
            #     trimester_months = [(trimester_start + i - 1) % 12 + 1
            #                         for i in range(3)]
            #     # do we have data in all months?
            #     if any(month not in available_months for month in trimester_months):
            #         continue
            #     # list of month-year whose month belong to current trimester
            #     months_of_xc = [mxc.month for mxc in xc.monthxcs
            #                     if mxc.month.m in trimester_months]

                # raw-clean FTAN on trimester data, using the vg curve
                # extracted from all data as initial guess
                try:
                    _, _, rawvg_control_period = xc.FTAN(
                        whiten=False,
                        control_periods=[control_period],
                        vgarray_init=rawvg_init,
                        optimize_curve=optimize_curve,
                        strength_smoothing=strength_smoothing,
                        use_inst_freq=use_inst_freq,
                        **kwargs)

                    phase_corr_control_period = xc.phase_func(vgcurve=rawvg_control_period)

                    _, _, cleanvg_control_period = xc.FTAN(
                        whiten=False,
                        phase_corr=phase_corr_control_period,
                        control_periods=[control_period],
                        vgarray_init=cleanvg_init,
                        optimize_curve=optimize_curve,
                        strength_smoothing=strength_smoothing,
                        use_inst_freq=use_inst_freq,
                        **kwargs)
                except pserrors.CannotCalculateInstFreq:
                    # skipping control period in case of pb with instantaneous frequency
                    continue

                # adding spectral SNRs associated with the periods of the
                # clean control period vg curve
                if add_SNRs:
                    cleanvg_control_period.add_SNRs(xc, control_periods=[control_period],
                                               vmin=vmin, vmax=vmax,
                                               signal2noise_trail=signal2noise_trail,
                                               noise_window_size=noise_window_size)

                # adding control period vg curve
                cleanvg.add_control_period(control_period, cleanvg_control_period)

        return rawampl, rawvg, cleanampl, cleanvg

    def phase_func(self, vgcurve):
        """
        Calculates the phase from the group velocity obtained
        using method self.FTAN, following the relationship:

        k(f) = 2.pi.integral[ 1/vg(f'), f'=f0..f ]
        phase(f) = distance.k(f)

        Returns the function phase: freq -> phase(freq)

        @param vgcurve: group velocity curve
        @type vgcurve: L{DispersionCurve}
        @rtype: L{scipy.interpolate.interpolate.interp1d}
        """
        freqarray = 1.0 / vgcurve.periods[::-1]
        vgarray = vgcurve.v[::-1]
        mask = ~np.isnan(vgarray)

        # array k[f]
        k = np.zeros_like(freqarray[mask])
        k[0] = 0.0
        k[1:] = 2 * np.pi * integrate.cumtrapz(y=1.0 / vgarray[mask], x=freqarray[mask])

        # array phi[f]
        phi = k * self.dist()

        # phase function of f
        return interp1d(x=freqarray[mask], y=phi)

    def plot_FTAN(self, rawampl=None, rawvg=None, cleanampl=None, cleanvg=None,
                  whiten=False, control_periods=None, showplot=True, normalize_ampl=True,
                  logscale=True, bbox=BBOX_SMALL, figsize=(16, 5), outfile=None,
                  vmin=SIGNAL_WINDOW_VMIN, vmax=SIGNAL_WINDOW_VMAX,
                  signal2noise_trail=SIGNAL2NOISE_TRAIL,
                  noise_window_size=NOISE_WINDOW_SIZE,
                  **kwargs):
        """
        Plots 4 panels related to frequency-time analysis:

        - 1st panel contains the cross-correlation (original, and bandpass
          filtered: see method self.plot_by_period_band)

        - 2nd panel contains an image of log(ampl^2) (or ampl) function of period
          T and group velocity vg, where ampl is the amplitude of the
          raw FTAN (basically, the amplitude of the envelope of the
          cross-correlation at time t = dist / vg, after applying a Gaussian
          bandpass filter centered at period T). The raw and clean dispersion
          curves (group velocity function of period) are also shown.

        - 3rd panel shows the same image, but for the clean FTAN (wherein the
          phase of the cross-correlation is corrected thanks to the raw
          dispersion curve). Also shown are the clean dispersion curve,
          the control-period dispersion curves, the standard deviation of the
          group velocity calculated from these 3-month dispersion curves
          and the SNR function of period.
          Only the velocities passing the default selection criteria
          (defined in the configuration file) are plotted.

        - 4th panel shows a small map with the pair of stations, with
          bounding box *bbox* = (min lon, max lon, min lat, max lat),
          and, if applicable, a plot of instantaneous vs nominal period

        The raw amplitude, raw dispersion curve, clean amplitude and clean
        dispersion curve of the FTAN are given in *rawampl*, *rawvg*,
        *cleanampl*, *cleanvg* (normally from method self.FTAN_complete).
        If not given, the FTAN is performed by calling self.FTAN_complete().

        Options:
        - Parameters *vmin*, *vmax*, *signal2noise_trail*, *noise_window_size*
          control the location of the signal window and the noise window
          (see function self.SNR()).
        - Set whiten=True to whiten the spectrum of the cross-correlation.
        - Set normalize_ampl=True to normalize the plotted amplitude (so
          that the max amplitude = 1 at each period).
        - Set logscale=True to plot log(ampl^2) instead of ampl.
        - Give a list of months in parameter *months* to perform the FTAN
          for a particular subset of months.
        - additional kwargs sent to *self.FTAN_complete*

        The method returns the plot figure.

        @param rawampl: 2D array containing the amplitude of the raw FTAN
        @type rawampl: L{numpy.ndarray}
        @param rawvg: raw dispersion curve
        @type rawvg: L{DispersionCurve}
        @param cleanampl: 2D array containing the amplitude of the clean FTAN
        @type cleanampl: L{numpy.ndarray}
        @param cleanvg: clean dispersion curve
        @type cleanvg: L{DispersionCurve}
        @type showplot: bool
        @param whiten: set to True to whiten the spectrum of the cross-correlation
        @type whiten: bool
        @param normalize_ampl: set to True to normalize amplitude
        @type normalize_ampl: bool
        @param months: list of months on which perform the FTAN (set to None to
                       perform the FTAN on all months)
        @type months: list of (L{MonthYear} or (int, int))
        @param logscale: set to True to plot log(ampl^2), to False to plot ampl
        @type logscale: bool
        @rtype: L{matplotlib.figure.Figure}
        """
        # performing FTAN analysis if needed
        if any(obj is None for obj in [rawampl, rawvg, cleanampl, cleanvg]):
            rawampl, rawvg, cleanampl, cleanvg = self.FTAN_complete(
                whiten=whiten, control_periods=control_periods, add_SNRs=True,
                vmin=vmin, vmax=vmax,
                signal2noise_trail=signal2noise_trail,
                noise_window_size=noise_window_size,
                **kwargs)

        if normalize_ampl:
            # normalizing amplitude at each period before plotting it
            # (so that the max = 1)
            for a in rawampl:
                a[...] /= a.max()
            for a in cleanampl:
                a[...] /= a.max()

        # preparing figure
        plt.ioff() # for some reason the figure shows, causing crashes in shell without display
        fig = plt.figure(figsize=figsize)

        # =======================================================
        # 1th panel: cross-correlation (original and band-passed)
        # =======================================================

        gs1 = gridspec.GridSpec(len(PERIOD_BANDS) + 1, 1, wspace=0.0, hspace=0.0)
        axlist = [fig.add_subplot(ss) for ss in gs1]
        self.plot_by_period_band(axlist=axlist, plot_title=False,
                                 whiten=whiten, control_periods=control_periods,
                                 vmin=vmin, vmax=vmax,
                                 signal2noise_trail=signal2noise_trail,
                                 noise_window_size=noise_window_size)

        # ===================
        # 2st panel: raw FTAN
        # ===================

        gs2 = gridspec.GridSpec(1, 1, wspace=0.2, hspace=0)
        ax = fig.add_subplot(gs2[0, 0])

        extent = (min(RAWFTAN_PERIODS), max(RAWFTAN_PERIODS),
                  min(FTAN_VELOCITIES), max(FTAN_VELOCITIES))
        m = np.log10(rawampl.transpose() ** 2) if logscale else rawampl.transpose()
        ax.imshow(m, aspect='auto', origin='lower', extent=extent, cmap=CMAP_SEISMIC) 

        # Period is instantaneous iif a list of (nominal period, inst period)
        # is associated with dispersion curve
        periodlabel = 'Instantaneous period (sec)' if rawvg.nom2inst_periods \
            else 'Nominal period (sec)'
        ax.set_xlabel(periodlabel)
        ax.set_ylabel("Velocity (km/sec)")
        # saving limits
        xlim = ax.get_xlim()
        ylim = ax.get_ylim()
        # raw & clean vg curves
        fmt = '--' if (~np.isnan(rawvg.v)).sum() > 1 else 'o'
        ax.plot(rawvg.periods, rawvg.v, fmt, color='blue',
                lw=2, label='raw disp curve')
        fmt = '-' if (~np.isnan(cleanvg.v)).sum() > 1 else 'o'
        ax.plot(cleanvg.periods, cleanvg.v, fmt, color='black',
                lw=2, label='clean disp curve')
        # plotting cut-off period
        cutoffperiod = self.dist() / 12.0
        ax.plot([cutoffperiod, cutoffperiod], ylim, color='grey')

        # setting legend and initial extent
        ax.legend(fontsize=11, loc='upper right')
        x = (xlim[0] + xlim[1]) / 2.0
        y = ylim[0] + 0.05 * (ylim[1] - ylim[0])
        ax.text(x, y, "Raw FTAN", fontsize=12,
                bbox={'edgecolor': 'black', 'facecolor': 'white', 'alpha': 0.5},
                horizontalalignment='center',
                verticalalignment='center')
        ax.set_xlim(xlim)
        ax.set_ylim(ylim)

        # ===========================
        # 3nd panel: clean FTAN + SNR
        # ===========================
        gs3 = gridspec.GridSpec(1, 1, wspace=0.2, hspace=0)
        ax = fig.add_subplot(gs3[0, 0])

        extent = (min(CLEANFTAN_PERIODS), max(CLEANFTAN_PERIODS),
                  min(FTAN_VELOCITIES), max(FTAN_VELOCITIES))
        m = np.log10(cleanampl.transpose() ** 2) if logscale else cleanampl.transpose()
        ax.imshow(m, aspect='auto', origin='lower', extent=extent, cmap=CMAP_SEISMIC) 
        # Period is instantaneous iif a list of (nominal period, inst period)
        # is associated with dispersion curve
        periodlabel = 'Instantaneous period (sec)' if cleanvg.nom2inst_periods \
            else 'Nominal period (sec)'
        ax.set_xlabel(periodlabel)
        ax.set_ylabel("Velocity (km/sec)")
        # saving limits
        xlim = ax.get_xlim()
        ylim = ax.get_ylim()

        # adding SNR function of period (on a separate y-axis)
        ax2 = ax.twinx()
        ax2.plot(cleanvg.periods, cleanvg.get_SNRs(xc=self), color='green', lw=2)
        # fake plot for SNR to appear in legend
        ax.plot([-1, 0], [0, 0], lw=2, color='green', label='SNR')
        ax2.set_ylabel('SNR', color='green')
        for tl in ax2.get_yticklabels():
            tl.set_color('green')

        # trimester vg curves
        ncontrol_period = len(cleanvg.v_control_periods)
        for i, vg_control_period in enumerate(cleanvg.filtered_control_period_vels()):
            label = '{} day disp curves (n={})'.format(str(int(CONTROL_PERIOD_LENGTH)), ncontrol_period) if i == 0 else None
            ax.plot(cleanvg.periods, vg_control_period, color='gray', label=label)

        # clean vg curve + error bars
        vels, sdevs = cleanvg.filtered_vels_sdevs()
        fmt = '-' if (~np.isnan(vels)).sum() > 1 else 'o'
        ax.errorbar(x=cleanvg.periods, y=vels, yerr=sdevs, fmt=fmt, color='black',
                    lw=2, label='clean disp curve')

        # plotting cut-off period
        cutoffperiod = self.dist() / 12.0
        ax.plot([cutoffperiod, cutoffperiod], ylim, color='grey')

        # legend
        ax.legend(fontsize=11, loc='upper right')
        x = (xlim[0] + xlim[1]) / 2.0
        y = ylim[0] + 0.05 * (ylim[1] - ylim[0])

        ax.text(x, y, "Clean FTAN", fontsize=12,
                bbox={'edgecolor': 'black', 'facecolor': 'white', 'alpha': 0.5},
                horizontalalignment='center',
                verticalalignment='center')

        # workaround for bug: data plots over legend when using Axes.twinx
        all_axes = fig.get_axes()
        for axis in all_axes:
            legend = axis.get_legend()
            if legend is not None:
                legend.remove()
                all_axes[-1].add_artist(legend)

        # setting initial extent
        ax.set_xlim(xlim)
        ax.set_ylim(ylim)

        # ===========================================
        # 4rd panel: tectonic provinces + pair (top),
        # instantaneous vs nominal period (bottom)
        # ===========================================

        # tectonic provinces and pairs
        gs4 = gridspec.GridSpec(1, 1, wspace=0.2, hspace=0.0)
        ax = fig.add_subplot(gs4[0, 0])

        psutils.basemap(ax, labels=False, axeslabels=False)
        x = (self.station1.coord[0], self.station2.coord[0])
        y = (self.station1.coord[1], self.station2.coord[1])
        s = (self.station1.name, self.station2.name)
        ax.plot(x, y, '^-', color='k', ms=10, mfc='w', mew=1)
        for lon, lat, label in zip(x, y, s):
            ax.text(lon, lat, label, ha='center', va='bottom', fontsize=7, weight='bold')
        ax.set_xlim(bbox[:2])
        ax.set_ylim(bbox[2:])

        # instantaneous vs nominal period (if applicable)
        gs5 = gridspec.GridSpec(1, 1, wspace=0.2, hspace=0.0)
        if rawvg.nom2inst_periods or cleanvg.nom2inst_periods:
            ax = fig.add_subplot(gs5[0, 0])
            if rawvg.nom2inst_periods:
                nomperiods, instperiods = zip(*rawvg.nom2inst_periods)
                ax.plot(nomperiods, instperiods, '-', label='raw FTAN')
            if cleanvg.nom2inst_periods:
                nomperiods, instperiods = zip(*cleanvg.nom2inst_periods)
                ax.plot(nomperiods, instperiods, '-', label='clean FTAN')

            ax.set_xlabel('Nominal period (s)')
            ax.set_ylabel('Instantaneous period (s)')
            ax.legend(fontsize=9, loc='lower right')
            ax.grid(True)

        # adjusting sizes
        gs1.update(left=0.03, right=0.25)
        gs2.update(left=0.30, right=0.535)
        gs3.update(left=0.585, right=0.81)
        gs4.update(left=0.85, right=0.98, bottom=0.51)
        gs5.update(left=0.87, right=0.98, top=0.48)

        # figure title, e.g., 'BL.GNSB-IU.RCBR, dist=1781 km, nslices=208'
        title = self._FTANplot_title(control_period=control_periods)
        fig.suptitle(title, fontsize=14)

        # exporting to file
        if outfile:
            fig.savefig(outfile, dpi=300, transparent=True)

        if showplot:
            plt.show()
        return fig

    def _plottitle(self, prefix='', control_period=None):
        """
        E.g., 'SPB-ITAB (365 timeslices from 2002-01-01 to 2002-12-01)'
           or 'SPB-ITAB (90 timeslices in months 01-2002, 02-2002)'
        """
        s = '{pref}{sta1}-{sta2} '
        s = s.format(pref=prefix, sta1=self.station1.name, sta2=self.station2.name)
        if not control_period:
            nslice = self.nslice
            s += '({} timeslices from {} to {})'.format(
                nslice, self.starttime.strftime('%d/%m/%Y'),
                self.endtime.strftime('%d/%m/%Y'))
        else:
            controlxcs = [cxc for cxc in self.controlxcs if cxc.control_period in control_period]
            nslice = sum(controlxcs.nslice for controlxcs in controlxcs)
            strmonths = ', '.join(str(c) for c in controlxcs)
            s += '{} timeslices in months {}'.format(nslice, strmonths)
        return s

    def _FTANplot_title(self, control_period=None):
        """
        E.g., 'BL.GNSB-IU.RCBR, dist=1781 km, nslices=208'
        """
        if not control_period:
            nslice = self.nslice
        else:
            nslice = sum(controlxc.nslice for controlxc in self.controlxcs
                       if controlxc.control_period in control_period)
        title = u"{}-{}, dist={:.0f} km, nslices={}"
        title = title.format(self.station1.network + '.' + self.station1.name,
                             self.station2.network + '.' + self.station2.name,
                             self.dist(), nslice)
        return title

    def _get_xcorr_dt(self):
        """
        Returns the interval of the time array.
        Warning: no check is made to ensure that that interval is constant.
        @rtype: float
        """
        return self.timearray[1] - self.timearray[0]

    def _get_xcorr_nmax(self):
        """
        Returns the max index of time array:
        - self.timearray = [-t[nmax] ... t[0] ... t[nmax]] if not symmetrized
        -                = [t[0] ... t[nmax-1] t[nmax]] if symmetrized
        @rtype: int
        """
        nt = len(self.timearray)
        return (nt - 1) / 2 if not self.symmetrized else nt - 1

    def _get_controlperiod_xcdataarray(self, control_periods=None):
        """
        Returns the sum of cross-corr data arrays of given
        list of (control_period) -- or the whole cross-corr if
        control_periods is None.

        @type control_periods: list of (int)
        @rtype: L{numpy.ndarray}
        """
        if not control_periods:
            return self.dataarray
        else:
            controlxcs = [cxc for cxc in self.controlxcs if cxc.control_period in control_periods]
            if controlxcs:
                return sum(controlxc.dataarray for controlxc in controlxcs)
            else:
                return None


class CrossCorrelationCollection(AttribDict):
    """
    Collection of cross-correlations
    = AttribDict{station1.name: AttribDict {station2.name: instance of CrossCorrelation}}

    AttribDict is a dict (defined in obspy.core) whose keys are also
    attributes. This means that a cross-correlation between a pair
    of stations STA01-STA02 can be accessed both ways:
    - self['STA01']['STA02'] (easier in regular code)
    - self.STA01.STA02 (easier in an interactive session)
    """

    def __init__(self):
        """
        Initializing object as AttribDict
        """
        AttribDict.__init__(self)

    def __repr__(self):
        npair = len(self.pairs())
        s = '(AttribDict)<Collection of cross-correlation between {0} pairs>'
        return s.format(npair)

    def pairs(self, sort=False, minslice=1, minSNR=None, mindist=None,
              withnets=None, onlywithnets=None, pairs_subset=None,
              **kwargs):
        """
        Returns pairs of stations of cross-correlation collection
        verifying conditions.

        Additional arguments in *kwargs* are sent to xc.SNR().

        @type sort: bool
        @type minslices: int
        @type minSNR: float
        @type mindist: float
        @type withnets: list of str
        @type onlywithnets: list of str
        @type pairs_subset: list of (str, str)
        @rtype: list of (str, str)
        """
        pairs = [(s1, s2) for s1 in self for s2 in self[s1]]
        if sort:
            pairs.sort()

        # filtering subset of pairs
        if pairs_subset:
            pairs_subset = [set(pair) for pair in pairs_subset]
            pairs = [pair for pair in pairs if set(pair) in pairs_subset]

        # filtering by nb of timeslices
        pairs = [(s1, s2) for (s1, s2) in pairs
                 if self[s1][s2].nslice >= minslice]

        # filtering by min SNR
        if minSNR:
            pairs = [(s1, s2) for (s1, s2) in pairs
                     if self[s1][s2].SNR(**kwargs) >= minSNR]

        # filtering by distance
        if mindist:
            pairs = [(s1, s2) for (s1, s2) in pairs
                     if self[s1][s2].dist() >= mindist]

        # filtering by network
        if withnets:
            # one of the station of the pair must belong to networks
            pairs = [(s1, s2) for (s1, s2) in pairs if
                     self[s1][s2].station1.network in withnets or
                     self[s1][s2].station2.network in withnets]
        if onlywithnets:
            # both stations of the pair must belong to networks
            pairs = [(s1, s2) for (s1, s2) in pairs if
                     self[s1][s2].station1.network in onlywithnets and
                     self[s1][s2].station2.network in onlywithnets]

        return pairs

    def pairs_and_SNRarrays(self, pairs_subset=None, minspectSNR=None,
                            whiten=False, verbose=False,
                            vmin=SIGNAL_WINDOW_VMIN, vmax=SIGNAL_WINDOW_VMAX,
                            signal2noise_trail=SIGNAL2NOISE_TRAIL,
                            noise_window_size=NOISE_WINDOW_SIZE):
        """
        Returns pairs and spectral SNR array whose spectral SNRs
        are all >= minspectSNR

        Parameters *vmin*, *vmax*, *signal2noise_trail*, *noise_window_size*
        control the location of the signal window and the noise window
        (see function self.SNR()).

        Returns {pair1: SNRarray1, pair2: SNRarray2 etc.}

        @type pairs_subset: list of (str, str)
        @type minspectSNR: float
        @type whiten: bool
        @type verbose: bool
        @rtype: dict from (str, str) to L{numpy.ndarray}
        """

        if verbose:
            print "Estimating spectral SNR of pair:",

        # initial list of pairs
        pairs = pairs_subset if pairs_subset else self.pairs()

        # filetring by min spectral SNR
        SNRarraydict = {}
        for (s1, s2) in pairs:
            if verbose:
                print '{0}-{1}'.format(s1, s2),

            SNRarray = self[s1][s2].SNR(periodbands=PERIOD_BANDS, whiten=whiten,
                                        vmin=vmin, vmax=vmax,
                                        signal2noise_trail=signal2noise_trail,
                                        noise_window_size=noise_window_size)
            if not minspectSNR or min(SNRarray) >= minspectSNR:
                SNRarraydict[(s1, s2)] = SNRarray

        if verbose:
            print

        return SNRarraydict

    def add(self, tracedict, stations, xcorr_tmax, xcorrdict=None, verbose=False):
        """
        Stacks cross-correlations between pairs of stations
        from a dict of {station.name: Trace} (in *tracedict*).

        You can provide pre-calculated cross-correlations in *xcorrdict*
        = dict {(station1.name, station2.name): numpy array containing cross-corr}

        Initializes self[station1][station2] as an instance of CrossCorrelation
        if the pair station1-station2 is not in self

        @type tracedict: dict from str to L{obspy.core.trace.Trace}
        @type stations: list of L{pysismo.psstation.Station}
        @type xcorr_tmax: float
        @type verbose: bool
        """
        if not xcorrdict:
            xcorrdict = {}

        stationtrace_pairs = it.combinations(sorted(tracedict.items()), 2)
        for (s1name, tr1), (s2name, tr2) in stationtrace_pairs:
            if verbose:
                print "{s1}-{s2}".format(s1=s1name, s2=s2name),

            # checking that sampling rates are equal
            assert abs(tr1.stats.sampling_rate - tr2.stats.sampling_rate) < EPS

            # looking for s1 and s2 in the list of stations
            station1 = next(s for s in stations if s.name == s1name)
            station2 = next(s for s in stations if s.name == s2name)

            # initializing self[s1] if s1 not in self
            # (avoiding setdefault() since behavior in unknown with AttribDict)
            if s1name not in self:
                self[s1name] = AttribDict()

            # initializing self[s1][s2] if s2 not in self[s1]
            if s2name not in self[s1name]:
                self[s1name][s2name] = CrossCorrelation(
                    station1=station1,
                    station2=station2,
                    xcorr_dt=1.0 / tr1.stats.sampling_rate,
                    xcorr_tmax=xcorr_tmax)

            # stacking cross-correlation
            try:
                # getting pre-calculated cross-corr, if provided
                xcorr = xcorrdict.get((s1name, s2name), None)
                self[s1name][s2name].add(tr1, tr2, xcorr=xcorr)
            except pserrors.NaNError:
                # got NaN
                s = "Warning: got NaN in cross-corr between {s1}-{s2} -> skipping"
                print s.format(s1=s1name, s2=s2name)

        if verbose:
            print

    def plot(self, stripheight=None, outfile=None, showplot=True):

        """
                method to plot a collection of cross-correlations !!

                @param stripheight: set the strip height of cross correlation in km. If not set, stripheight is
                                    automatically determined.
                @type stripheight:  float
                @param outfile:     If set, path to save plot to. File extension determines file format.
                @type outfile:      str
                @type showplot:     bool
                """

        pairs = self.pairs()
        maxdist = max(self[x][y].dist() for (x, y) in pairs)
        mindist = min(self[x][y].dist() for (x, y) in pairs)
        maxtime = max(max(self[x][y].timearray) for (x, y) in pairs)
        mintime = min(min(self[x][y].timearray) for (x, y) in pairs)
        corrlength = max(len(self[x][y].dataarray) for (x, y) in pairs)

        if not stripheight:
            stripheight = (maxdist - mindist) / 300

        Y = np.zeros(shape=(int(math.ceil(maxdist / stripheight)) + 1, corrlength))
        pairs.sort(key=lambda (s1, s2): self[s1][s2].dist())

        for ipairs, (s1, s2) in enumerate(pairs):
            # data = psutils.bandpass_butterworth(data=xc[s1][s2].dataarray,dt=1.0,periodmin=8.0,periodmax=40.0)
            data = self[s1][s2].dataarray / (max(abs(self[s1][s2].dataarray)))
            ypos = int(round(self[s1][s2].dist() / stripheight))
            Y[ypos] = data

        # plt.figure()
        plt.imshow(Y, vmin=-1., vmax=1., cmap='seismic', aspect='auto', origin='lower',
                   extent=(mintime, maxtime, 0, maxdist + stripheight))
        plt.xlim(-40, 40)
        plt.xlabel('Time (s)')
        plt.ylabel('inter-station distance (km)')

        if outfile:
            plt.savefig(outfile)

        if showplot:
            plt.show()

    def plot_spectral_SNR(self, whiten=False, minSNR=None, minspectSNR=None,
                          minslice=1, mindist=None, withnets=None, onlywithnets=None,
                          vmin=SIGNAL_WINDOW_VMIN, vmax=SIGNAL_WINDOW_VMAX,
                          signal2noise_trail=SIGNAL2NOISE_TRAIL,
                          noise_window_size=NOISE_WINDOW_SIZE):
        """
        Plots spectral SNRs
        """

        # filtering pairs
        pairs = self.pairs(minslice=minslice, minSNR=minSNR, mindist=mindist,
                           withnets=withnets, onlywithnets=onlywithnets,
                           vmin=vmin, vmax=vmax,
                           signal2noise_trail=signal2noise_trail,
                           noise_window_size=noise_window_size)

        # SNRarrays = dict {(station1,station2): SNR array}
        SNRarrays = self.pairs_and_SNRarrays(
            pairs_subset=pairs, minspectSNR=minspectSNR,
            whiten=whiten, verbose=True,
            vmin=vmin, vmax=vmax,
            signal2noise_trail=signal2noise_trail,
            noise_window_size=noise_window_size)

        npair = len(SNRarrays)
        if not npair:
            print 'Nothing to plot!!!'
            return

        # min-max SNR
        minSNR = min([SNR for SNRarray in SNRarrays.values() for SNR in SNRarray])
        maxSNR = max([SNR for SNRarray in SNRarrays.values() for SNR in SNRarray])

        # sorting SNR arrays by increasing first value
        SNRarrays = OrderedDict(sorted(SNRarrays.items(), key=lambda (k, v): v[0]))

        # array of mid of time bands
        periodarray = [(tmin + tmax) / 2.0 for (tmin, tmax) in PERIOD_BANDS]
        minperiod = min(periodarray)

        # color cycle
        cc = mpl.rcParams['axes.prop_cycle']

        # plotting SNR arrays
        plt.figure()
        for ipair, ((s1, s2), SNRarray) in enumerate(SNRarrays.items()):
            xc = self[s1][s2]
            #color = cc[ipair % len(cc)]
            color = cc.by_key()['color'][ipair % len(cc)]

            # SNR vs period
            plt.plot(periodarray, SNRarray, color=color)

            # annotation
            xtext = minperiod - 4
            ytext = minSNR * 0.5 + ipair * (maxSNR - minSNR * 0.5) / (npair - 1)
            xytext = (xtext, ytext)
            xyarrow = (minperiod - 1, SNRarray[0])
            relpos = (1, 0.5)
            net1 = xc.station1.network
            net2 = xc.station2.network

            s = '{i}: {net1}.{s1}-{net2}.{s2}: {dist:.1f} km, {nslice} timeslices'
            s = s.format(i=ipair, net1=net1, s1=s1, net2=net2, s2=s2,
                         dist=xc.dist(), nslice=xc.nslice)

            bbox = {'color': color, 'facecolor': 'white', 'alpha': 0.9}
            arrowprops = {'arrowstyle': '-', 'relpos': relpos, 'color': color}

            plt.annotate(s=s, xy=xyarrow, xytext=xytext, fontsize=9,
                         color='k', horizontalalignment='right',
                         bbox=bbox, arrowprops=arrowprops)

        plt.xlim((0.0, plt.xlim()[1]))
        plt.xlabel('Period (s)')
        plt.ylabel('SNR')
        plt.title(u'{0} pairs'.format(npair))
        plt.grid()
        plt.show()

    def plot_pairs(self, minSNR=None, minspectSNR=None, minslice=1, mindist=None,
                   withnets=None, onlywithnets=None, pairs_subset=None, whiten=False,
                   stationlabel=False, bbox=BBOX_LARGE, xsize=10, plotkwargs=None,
                   SNRkwargs=None):
        """
        Plots pairs of stations on a map
        @type bbox: tuple
        """
        if not plotkwargs:
            plotkwargs = {}
        if not SNRkwargs:
            SNRkwargs = {}

        # filtering pairs
        pairs = self.pairs(minslice=minslice, minSNR=minSNR, mindist=mindist,
                           withnets=withnets, onlywithnets=onlywithnets,
                           pairs_subset=pairs_subset, **SNRkwargs)

        if minspectSNR:
            # plotting only pairs with all spect SNR >= minspectSNR
            SNRarraydict = self.pairs_and_SNRarrays(
                pairs_subset=pairs, minspectSNR=minspectSNR,
                whiten=whiten, verbose=True, **SNRkwargs)
            pairs = SNRarraydict.keys()

        # nb of pairs
        npair = len(pairs)
        if not npair:
            print 'Nothing to plot!!!'
            return

        # initializing figure
        aspectratio = (bbox[3] - bbox[2]) / (bbox[1] - bbox[0])
        plt.figure(figsize=(xsize, aspectratio * xsize))

        # plotting coasts and tectonic provinces
        psutils.basemap(plt.gca(), bbox=bbox)

        # plotting pairs
        for s1, s2 in pairs:
            x, y = zip(self[s1][s2].station1.coord, self[s1][s2].station2.coord)
            if not plotkwargs:
                plotkwargs = dict(color='grey', lw=0.5)
            plt.plot(x, y, '-', **plotkwargs)

        # plotting stations
        x, y = zip(*[s.coord for s in self.stations(pairs)])
        plt.plot(x, y, '^', color='k', ms=10, mfc='w', mew=1)
        if stationlabel:
            # stations label
            for station in self.stations(pairs):
                plt.text(station.coord[0], station.coord[1], station.name,
                         ha='center', va='bottom', fontsize=10, weight='bold')

        # setting axes
        plt.title(u'{0} pairs'.format(npair))
        plt.xlim(bbox[:2])
        plt.ylim(bbox[2:])
        plt.show()

    def export(self, outprefix, stations=None, verbose=False):
        """
        Exports cross-correlations to picke file and txt file

        @type outprefix: str or unicode
        @type stations: list of L{Station}
        """
        self._to_picklefile(outprefix, verbose=verbose)
        self._to_ascii(outprefix, verbose=verbose)
        self._pairsinfo_to_ascii(outprefix, verbose=verbose)
        self._stationsinfo_to_ascii(outprefix, stations=stations, verbose=verbose)

    def FTANs(self, prefix=None, suffix='', whiten=False,
              normalize_ampl=True, logscale=True, mindist=None,
              minSNR=None, minspectSNR=None, control_periods=None,
              vmin=SIGNAL_WINDOW_VMIN, vmax=SIGNAL_WINDOW_VMAX,
              signal2noise_trail=SIGNAL2NOISE_TRAIL,
              noise_window_size=NOISE_WINDOW_SIZE,
              **kwargs):
        """
        Exports raw-clean FTAN plots to pdf (one page per pair)
        and clean dispersion curves to pickle file by calling
        plot_FTAN() for each cross-correlation.

        pdf is exported to *prefix*[_*suffix*].pdf
        dispersion curves are exported to *prefix*[_*suffix*].pickle

        If *prefix* is not given, then it is automatically set up as:
        *FTAN_DIR*/FTAN[_whitenedxc][_mindist=...][_minsSNR=...]
                       [_minspectSNR=...][_month-year_month-year]

        e.g.: ./output/FTAN/FTAN_whitenedxc_minspectSNR=10

        Options:
        - Parameters *vmin*, *vmax*, *signal2noise_trail*, *noise_window_size*
          control the location of the signal window and the noise window
          (see function xc.SNR()).
        - Set whiten=True to whiten the spectrum of the cross-correlation.
        - Set normalize_ampl=True to normalize the plotted amplitude (so
          that the max amplitude = 1 at each period).
        - Set logscale=True to plot log(ampl^2) instead of ampl.
        - additional kwargs sent to FTAN_complete() and plot_FTAN()

        See. e.g., Levshin & Ritzwoller, "Automated detection,
        extraction, and measurement of regional surface waves",
        Pure Appl. Geoph. (2001) and Bensen et al., "Processing
        seismic ambient noise data to obtain reliable broad-band
        surface wave dispersion measurements", Geophys. J. Int. (2007).

        @type prefix: str or unicode
        @type suffix: str or unicode
        @type minSNR: float
        @type mindist: float
        @type minspectSNR: float
        @type whiten: bool
        @type control_periods: list of (int, int)
        """
        # setting default prefix if not given
        if not prefix:
            parts = [os.path.join(FTAN_DIR, 'FTAN')]
            if whiten:
                parts.append('whitenedxc')
            if mindist:
                parts.append('mindist={}'.format(mindist))
            if minSNR:
                parts.append('minSNR={}'.format(minSNR))
            if minspectSNR:
                parts.append('minspectSNR={}'.format(minspectSNR))
            if control_periods:
                parts.extend('control_period={}'.format(c) for c in control_periods)
        else:
            parts = [prefix]
        if suffix:
            parts.append(suffix)

        # path of output files (without extension)
        outputpath = u'_'.join(parts)

        # opening pdf file
        pdfpath = u'{}.pdf'.format(outputpath)
        if os.path.exists(pdfpath):
            # backup
            shutil.copyfile(pdfpath, pdfpath + '~')
        pdf = PdfPages(pdfpath)

        # filtering pairs
        pairs = self.pairs(sort=True, minSNR=minSNR, mindist=mindist,
                           vmin=vmin, vmax=vmax,
                           signal2noise_trail=signal2noise_trail,
                           noise_window_size=noise_window_size)
        if minspectSNR:
            # plotting only pairs with all spect SNR >= minspectSNR
            SNRarraydict = self.pairs_and_SNRarrays(
                pairs_subset=pairs, minspectSNR=minspectSNR,
                whiten=whiten, verbose=True,
                vmin=vmin, vmax=vmax,
                signal2noise_trail=signal2noise_trail,
                noise_window_size=noise_window_size)
            pairs = sorted(SNRarraydict.keys())

        s = ("Exporting FTANs of {0} pairs to file {1}.pdf\n"
             "and dispersion curves to file {1}.pickle\n")
        print s.format(len(pairs), outputpath)

        cleanvgcurves = []
        print "Appending FTAN of pair:",
        for i, (s1, s2) in enumerate(pairs):
            # appending FTAN plot of pair s1-s2 to pdf
            print "[{}] {}-{}".format(i + 1, s1, s2),
            xc = self[s1][s2]
            assert isinstance(xc, CrossCorrelation)

            try:
                # complete FTAN analysis
                rawampl, rawvg, cleanampl, cleanvg = xc.FTAN_complete(
                    whiten=whiten, control_periods=control_periods,
                    vmin=vmin, vmax=vmax,
                    signal2noise_trail=signal2noise_trail,
                    noise_window_size=noise_window_size,
                    **kwargs)

                # plotting raw-clean FTAN
                fig = xc.plot_FTAN(rawampl, rawvg, cleanampl, cleanvg,
                                   whiten=whiten,
                                   normalize_ampl=normalize_ampl,
                                   logscale=logscale,
                                   showplot=False,
                                   vmin=vmin, vmax=vmax,
                                   signal2noise_trail=signal2noise_trail,
                                   noise_window_size=noise_window_size,
                                   **kwargs)
                pdf.savefig(fig)
                plt.close()

                # appending clean vg curve
                cleanvgcurves.append(cleanvg)

            except Exception as err:
                # something went wrong with this FTAN
                print "\nGot unexpected error:\n\n{}\n\nSKIPPING PAIR!".format(err)

        print "\nSaving files..."

        # closing pdf
        pdf.close()

        # exporting vg curves to pickle file
        f = psutils.openandbackup(outputpath + '.pickle', mode='wb')
        pickle.dump(cleanvgcurves, f, protocol=2)
        f.close()

    def stations(self, pairs, sort=True):
        """
        Returns a list of unique stations corresponding
        to a list of pairs (of station name).

        @type pairs: list of (str, str)
        @rtype: list of L{pysismo.psstation.Station}
        """
        stations = []
        for s1, s2 in pairs:
            if self[s1][s2].station1 not in stations:
                stations.append(self[s1][s2].station1)
            if self[s1][s2].station2 not in stations:
                stations.append(self[s1][s2].station2)

        if sort:
            stations.sort(key=lambda obj: obj.name)

        return stations

    def filter_by_distance(self, dmin=None, dmax=None):
        '''
        Returns a new CrossCorrelationCollection containing station pairs filtered by distance

        @type dmin: minimum interstation distance (km)
        @type dmax: maximum interstation distance (km)
        '''

        xc = self
        xcr = xc.copy()

        if dmin and dmax:
            for s1 in xc:
                for s2 in xc[s1]:
                    if xc[s1][s2].dist() <= dmin or xc[s1][s2].dist() > dmax:
                        del(xcr[s1][s2])

        elif not dmin and dmax:
            for s1 in xc:
                for s2 in xc[s1]:
                    if xc[s1][s2].dist() > dmax:
                        del(xcr[s1][s2])

        elif not dmax and dmin:
            for s1 in xc:
                for s2 in xc[s1]:
                    if xc[s1][s2].dist() <= dmin:
                        del(xcr[s1][s2])

        else:
            xcr = xc

        return xcr

    def _to_picklefile(self, outprefix, verbose=False):
        """
        Dumps cross-correlations to (binary) pickle file

        @type outprefix: str or unicode
        """
        if verbose:
            s = "Exporting cross-correlations in binary format to file: {}.pickle"
            print s.format(outprefix)

        f = psutils.openandbackup(outprefix + '.pickle', mode='wb')
        pickle.dump(self, f, protocol=2)
        f.close()

    def _to_ascii(self, outprefix, verbose=False):
        """
        Exports cross-correlations to txt file

        @type outprefix: str or unicode
        """
        if verbose:
            s = "Exporting cross-correlations in ascci format to file: {}.txt"
            print s.format(outprefix)

        # writing data file: time array (1st column)
        # and cross-corr array (one column per pair)
        f = psutils.openandbackup(outprefix + '.txt', mode='w')
        pairs = [(s1, s2) for (s1, s2) in self.pairs(sort=True) if self[s1][s2].nslice]

        # writing header
        header = ['time'] + ["{0}-{1}".format(s1, s2) for s1, s2 in pairs]
        f.write('\t'.join(header) + '\n')

        # writing line = ith [time, cross-corr 1st pair, cross-corr 2nd pair etc]
        data = zip(self._get_timearray(), *[self[s1][s2].dataarray for s1, s2 in pairs])
        for fields in data:
            line = [str(fld) for fld in fields]
            f.write('\t'.join(line) + '\n')
        f.close()

    def _pairsinfo_to_ascii(self, outprefix, verbose=False):
        """
        Exports pairs information to txt file

        @type outprefix: str or unicode
        """
        if verbose:
            s = "Exporting pairs information to file: {}.stats.txt"
            print s.format(outprefix)

        # writing file: coord, locations, ids etc. for each pair
        pairs = self.pairs(sort=True)
        f = psutils.openandbackup(outprefix + '.stats.txt', mode='w')
        # header
        header = ['pair', 'lon1', 'lat1', 'lon2', 'lat2',
                  'locs1', 'locs2', 'ids1', 'ids2',
                  'distance', 'starttime', 'endtime', 'nslice']
        f.write('\t'.join(header) + '\n')

        # fields
        for (s1, s2) in pairs:
            fields = [
                '{0}-{1}'.format(s1, s2),
                self[s1][s2].station1.coord[0],
                self[s1][s2].station1.coord[1],
                self[s1][s2].station2.coord[0],
                self[s1][s2].station2.coord[1],
                ','.join(sorted("'{}'".format(l) for l in self[s1][s2].locs1)),
                ','.join(sorted("'{}'".format(l) for l in self[s1][s2].locs2)),
                ','.join(sorted(sid for sid in self[s1][s2].ids1)),
                ','.join(sorted(sid for sid in self[s1][s2].ids2)),
                self[s1][s2].dist(),
                self[s1][s2].starttime,
                self[s1][s2].endtime,
                self[s1][s2].nslice
            ]
            line = [str(fld) if (fld or fld == 0) else 'none' for fld in fields]
            f.write('\t'.join(line) + '\n')

        f.close()

    def _stationsinfo_to_ascii(self, outprefix, stations=None, verbose=False):
        """
        Exports information on cross-correlated stations
        to txt file

        @type outprefix: str or unicode
        @type stations: list of {Station}
        """
        if verbose:
            s = "Exporting stations information to file: {}.stations.txt"
            print s.format(outprefix)

        if not stations:
            # extracting the list of stations from cross-correlations
            # if not provided
            stations = self.stations(self.pairs(minslice=0), sort=True)

        # opening stations file and writing:
        # station name, network, lon, lat, nb of pairs, total timeslices of cross-corr
        f = psutils.openandbackup(outprefix + '.stations.txt', mode='w')
        header = ['name', 'network', 'lon', 'lat', 'npair', 'nslice']
        f.write('\t'.join(header) + '\n')

        for station in stations:
            # pairs in which station appears
            pairs = [(s1, s2) for s1, s2 in self.pairs()
                     if station in [self[s1][s2].station1, self[s1][s2].station2]]
            # total nb of timeslices of pairs
            nslice = sum(self[s1][s2].nslice for s1, s2 in pairs)
            # writing fields
            fields = [
                station.name,
                station.network,
                str(station.coord[0]),
                str(station.coord[1]),
                str(len(pairs)),
                str(nslice)
            ]
            f.write('\t'.join(fields) + '\n')

        f.close()

    def _get_timearray(self):
        """
        Returns time array of cross-correlations

        @rtype: L{numpy.ndarray}
        """

        pairs = self.pairs()

        # reference time array
        s1, s2 = pairs[0]
        reftimearray = self[s1][s2].timearray

        # checking that all time arrays are equal to reference time array
        for (s1, s2) in pairs:
            if np.any(self[s1][s2].timearray != reftimearray):
                s = 'Cross-corr collection does not have a unique timelag array'
                raise Exception(s)

        return reftimearray


def get_merged_trace(station, starttime, endtime, skiplocs=CROSSCORR_SKIPLOCS, minfill=MINFILL):
    """
    Returns one trace extracted from selected station, at selected time slice

    Traces whose location belongs to *skiplocs* are discarded, then
    if several locations remain, only the first is kept. Finally,
    if several traces (with the same location) remain, they are
    merged, WITH GAPS FILLED USING LINEAR INTERPOLATION.

    Raises CannotPreprocess exception if:
    - no trace remain after discarded the unwanted locations
    - data fill is < *minfill*

    @type station: L{psstation.Station}
    @type starttime: list of time slice start times
    @type endtime: list of time slice start times
    @param skiplocs: list of locations to discard in station's data
    @type skiplocs: iterable
    @param minfill: minimum data fill to keep trace
    @rtype: L{Trace}
    """

    # getting station's stream at selected time slice
    tstart = UTCDateTime(starttime)
    tend = UTCDateTime(endtime)
    st = read(pathname_or_url=station.getpath(tstart),
              starttime=tstart,
              endtime=tend)

    # removing traces of stream from locations to skip
    for tr in [tr for tr in st if tr.stats.location in skiplocs]:
        st.remove(tr)

    if not st.traces:
        # no remaining trace!
        raise pserrors.CannotPreprocess("No trace")

    # if more than one location, we retain only the first one
    if len(set(tr.id for tr in st)) > 1:
        select_loc = sorted(set(tr.stats.location for tr in st))[0]
        for tr in [tr for tr in st if tr.stats.location != select_loc]:
            st.remove(tr)

    # Data fill for current date
    fill = psutils.get_fill(st, starttime=tstart, endtime=tend)
    if fill < minfill:
        # not enough data
        raise pserrors.CannotPreprocess("{:.0f}% fill".format(fill * 100))

    # Merging traces, FILLING GAPS WITH LINEAR INTERP
    st.merge(fill_value='interpolate')
    trace = st[0]
    return trace


def get_or_attach_response(trace, dataless_inventories=(), xml_inventories=()):
    """
    Returns or attach instrumental response, from dataless seed inventories
    (as returned by psstation.get_dataless_inventories()) and/or StationXML
    inventories (as returned by psstation.get_stationxml_inventories()).
    If a response if found in a dataless inventory, then a dict of poles
    and zeros is returned. If a response is found in a StationXML
    inventory, then it is directly attached to the trace and nothing is
    returned.

    Raises CannotPreprocess exception if no instrumental response is found.

    @type trace: L{Trace}
    @param dataless_inventories: inventories from dataless seed files (as returned by
                                 psstation.get_dataless_inventories())
    @type dataless_inventories: list of L{obspy.xseed.parser.Parser}
    @param xml_inventories: inventories from StationXML files (as returned by
                            psstation.get_stationxml_inventories())
    @type xml_inventories: list of L{obspy.station.inventory.Inventory}
    """

    # looking for instrument response...
    try:
        # ...first in dataless seed inventories
        paz = psstation.get_paz(channelid=trace.id,
                                t=trace.stats.starttime,
                                inventories=dataless_inventories)
        return paz
    except pserrors.NoPAZFound:
        # ... then in dataless seed inventories, replacing 'BHZ' with 'HHZ'
        # in trace's id (trick to make code work with Diogo's data)
        try:
            paz = psstation.get_paz(channelid=trace.id.replace('BHZ', 'HHZ'),
                                    t=trace.stats.starttime,
                                    inventories=dataless_inventories)
            return paz
        except pserrors.NoPAZFound:
            # ...finally in StationXML inventories
            try:
                trace.attach_response(inventories=xml_inventories)
            except:
                # no response found!
                raise pserrors.CannotPreprocess("No response found")


def preprocess_trace(trace, paz=None, freqmin=FREQMIN, freqmax=FREQMAX,
                     freqmin_earthquake=FREQMIN_EARTHQUAKE,
                     freqmax_earthquake=FREQMAX_EARTHQUAKE,
                     corners=CORNERS, zerophase=ZEROPHASE,
                     period_resample=PERIOD_RESAMPLE,
                     onebit_norm=ONEBIT_NORM,
                     window_time=WINDOW_TIME,
                     window_freq=WINDOW_FREQ,
                     starttime=None,
                     endtime=None):
    """
    Preprocesses a trace (so that it is ready to be cross-correlated),
    by applying the following steps:
    - removal of instrument response, mean and trend
    - band-pass filtering between *freqmin*-*freqmax*
    - downsampling to *period_resample* secs
    - time-normalization (one-bit normalization or normalization
      by the running mean in the earthquake frequency band)
    - spectral whitening (if running mean normalization)

    Raises CannotPreprocess exception if:
    - trace only contains 0 (happens sometimes...)
    - a normalization weight is 0 or NaN
    - a Nan appeared in trace data

    Note that the processing steps are performed in-place.

    @type trace: L{Trace}
    @param paz: poles and zeros of instrumental response
                (set None if response is directly attached to trace)
    @param freqmin: low frequency of the band-pass filter
    @param freqmax: high frequency of the band-pass filter
    @param freqmin_earthquake: low frequency of the earthquake band
    @param freqmax_earthquake: high frequency of the earthquake band
    @param corners: nb or corners of the band-pass filter
    @param zerophase: set to True for filter not to shift phase
    @type zerophase: bool
    @param period_resample: resampling period in seconds
    @param onebit_norm: set to True to apply one-bit normalization (else,
                        running mean normalization is applied)
    @type onebit_norm: bool
    @param window_time: width of the window to calculate the running mean
                        in the earthquake band (for the time-normalization)
    @param window_freq: width of the window to calculate the running mean
                        of the amplitude spectrum (for the spectral whitening)
    @param starttime: UTCDateTime of trace start time
    @param endttime: UTCDateTime of trace end time
    """

    # ============================================
    # Removing instrument response, mean and trend
    # ============================================

    # resample
    trace.interpolate(1/period_resample, method='linear')

    # demean and detrend before preprocessing - quick patch, may need revision !!
    trace.detrend(type='constant')
    trace.detrend(type='linear')

    # removing response...
    if paz:
        # ...using paz:
        if trace.stats.sampling_rate > 10.0:
            # decimating large trace, else fft crashes
            factor = int(np.ceil(trace.stats.sampling_rate / 10))
            trace.decimate(factor=factor, no_filter=True)
        trace.simulate(paz_remove=paz,
                       paz_simulate=obspy.signal.cornFreq2Paz(0.01),
                       remove_sensitivity=True,
                       simulate_sensitivity=True,
                       nfft_pow2=True)
    else:
        # ...using StationXML:
        trace.remove_response(output="VEL", zero_mean=True)

    # demeaning, detrending
    trace.detrend(type='constant')
    trace.detrend(type='linear')

    if np.all(trace.data == 0.0):
        # no data -> skipping trace
        raise pserrors.CannotPreprocess("Only zeros")

    # =========
    # Band-pass
    # =========
    # keeping a copy of the trace to calculate weights of time-normalization
    trcopy = trace.copy()

    # band-pass
    trace.filter(type="bandpass",
                 freqmin=freqmin,
                 freqmax=freqmax,
                 corners=corners,
                 zerophase=zerophase)

    # ==================
    # Time normalization
    # ==================
    if onebit_norm:
        # one-bit normalization
        trace.data = np.sign(trace.data)
    else:
        # normalization of the signal by the running mean
        # in the earthquake frequency band
        trcopy.filter(type="bandpass",
                      freqmin=freqmin_earthquake,
                      freqmax=freqmax_earthquake,
                      corners=corners,
                      zerophase=zerophase)

        # Time-normalization weights from smoothed abs(data)
        # Note that trace's data can be a masked array
        halfwindow = int(round(window_time * trcopy.stats.sampling_rate / 2))
        mask = ~trcopy.data.mask if np.ma.isMA(trcopy.data) else None
        tnorm_w = psutils.moving_avg(np.abs(trcopy.data),
                                     halfwindow=halfwindow,
                                     mask=mask)
        if np.ma.isMA(trcopy.data):
            # turning time-normalization weights into a masked array
            s = "[warning: {}.{} trace's data is a masked array]"
            print s.format(trace.stats.network, trace.stats.station),
            tnorm_w = np.ma.masked_array(tnorm_w, trcopy.data.mask)

        if np.any((tnorm_w == 0.0) | np.isnan(tnorm_w)):
            # illegal normalizing value -> skipping trace
            raise pserrors.CannotPreprocess("Zero or NaN normalization weight")

        # time-normalization
        trace.data /= tnorm_w

        # ==================
        # Spectral whitening
        # ==================
        fft = rfft(trace.data)  # real FFT
        deltaf = trace.stats.sampling_rate / trace.stats.npts  # frequency step
        # smoothing amplitude spectrum
        halfwindow = int(round(window_freq / deltaf / 2.0))
        weight = psutils.moving_avg(abs(fft), halfwindow=halfwindow)
        # normalizing spectrum and back to time domain
        trace.data = irfft(fft / weight, n=len(trace.data))
        # re bandpass to avoid low/high freq noise
        trace.filter(type="bandpass",
                     freqmin=freqmin,
                     freqmax=freqmax,
                     corners=corners,
                     zerophase=zerophase)

    # Verifying that we don't have nan in trace data
    if np.any(np.isnan(trace.data)):
        raise pserrors.CannotPreprocess("Got NaN in trace data")

    # zero pad to starttime and endtime to prevent xcorr miss-alignment
    trace.trim(starttime=starttime, endtime=endtime, pad=True, fill_value=0)

def load_pickled_xcorr(pickle_file):
    """
    Loads pickle-dumped cross-correlations

    @type pickle_file: str or unicode
    @rtype: L{CrossCorrelationCollection}
    """
    f = open(name=pickle_file, mode='rb')
    xc = pickle.load(f)
    f.close()
    return xc


def load_pickled_xcorr_interactive(xcorr_dir=CROSSCORR_DIR, xcorr_files='xcorr*.pickle*'):
    """
    Loads interactively pickle-dumped cross-correlations, by giving the user
    a choice among a list of file matching xcorrFiles

    @type xcorr_dir: str or unicode
    @type xcorr_files: str or unicode
    @rtype: L{CrossCorrelationCollection}
    """

    # looking for files that match xcorrFiles
    pathxcorr = os.path.join(xcorr_dir, xcorr_files)
    flist = glob.glob(pathname=pathxcorr)
    flist.sort()

    pickle_file = None
    if len(flist) == 1:
        pickle_file = flist[0]
        print 'Reading cross-correlation from file ' + pickle_file
    elif len(flist) > 0:
        print 'Select file containing cross-correlations:'
        print '\n'.join('{i} - {f}'.format(i=i, f=os.path.basename(f))
                        for (i, f) in enumerate(flist))
        i = int(raw_input('\n'))
        pickle_file = flist[i]

    # loading cross-correlations
    xc = load_pickled_xcorr(pickle_file=pickle_file)

    return xc


def FTAN(x, dt, periods, alpha, phase_corr=None):
    """
    Frequency-time analysis of a time series.
    Calculates the Fourier transform of the signal (xarray),
    calculates the analytic signal in frequency domain,
    applies Gaussian bandpass filters centered around given
    center periods, and calculates the filtered analytic
    signal back in time domain.
    Returns the amplitude/phase matrices A(f0,t) and phi(f0,t),
    that is, the amplitude/phase function of time t of the
    analytic signal filtered around period T0 = 1 / f0.

    See. e.g., Levshin & Ritzwoller, "Automated detection,
    extraction, and measurement of regional surface waves",
    Pure Appl. Geoph. (2001) and Bensen et al., "Processing
    seismic ambient noise data to obtain reliable broad-band
    surface wave dispersion measurements", Geophys. J. Int. (2007).

    @param dt: sample spacing
    @type dt: float
    @param x: data array
    @type x: L{numpy.ndarray}
    @param periods: center periods around of Gaussian bandpass filters
    @type periods: L{numpy.ndarray} or list
    @param alpha: smoothing parameter of Gaussian filter
    @type alpha: float
    @param phase_corr: phase correction, function of freq
    @type phase_corr: L{scipy.interpolate.interpolate.interp1d}
    @rtype: (L{numpy.ndarray}, L{numpy.ndarray})
    """

    # Initializing amplitude/phase matrix: each column =
    # amplitude function of time for a given Gaussian filter
    # centered around a period
    amplitude = np.zeros(shape=(len(periods), len(x)))
    phase = np.zeros(shape=(len(periods), len(x)))

    # Fourier transform
    Xa = fft(x)
    # aray of frequencies
    freq = fftfreq(len(Xa), d=dt)

    # analytic signal in frequency domain:
    #         | 2X(f)  for f > 0
    # Xa(f) = | X(f)   for f = 0
    #         | 0      for f < 0
    # with X = fft(x)
    Xa[freq < 0] = 0.0
    Xa[freq > 0] *= 2.0

    # applying phase correction: replacing phase with given
    # phase function of freq
    if phase_corr:
        # doamin of definition of phase_corr(f)
        minfreq = phase_corr.x.min()
        maxfreq = phase_corr.x.max()
        mask = (freq >= minfreq) & (freq <= maxfreq)

        # replacing phase with user-provided phase correction:
        # updating Xa(f) as |Xa(f)|.exp(-i.phase_corr(f))
        phi = phase_corr(freq[mask])
        Xa[mask] = np.abs(Xa[mask]) * np.exp(-1j * phi)

        # tapering
        taper = cosine_taper(npts=mask.sum(), p=0.05)
        Xa[mask] *= taper
        Xa[~mask] = 0.0

    # applying narrow bandpass Gaussian filters
    for iperiod, T0 in enumerate(periods):
        # bandpassed analytic signal
        f0 = 1.0 / T0
        Xa_f0 = Xa * np.exp(-alpha * ((freq - f0) / f0) ** 2)
        # back to time domain
        xa_f0 = ifft(Xa_f0)
        # filling amplitude and phase of column
        amplitude[iperiod, :] = np.abs(xa_f0)
        phase[iperiod, :] = np.angle(xa_f0)

    return amplitude, phase


def extract_dispcurve(amplmatrix, velocities, periodmask=None, varray_init=None,
                      optimizecurve=True, strength_smoothing=STRENGTH_SMOOTHING):
    """
    Extracts a disperion curve (velocity vs period) from an amplitude
    matrix *amplmatrix*, itself obtained from FTAN.

    Among the curves that ride along local maxima of amplitude,
    the selected group velocity curve v(T) maximizes the sum of
    amplitudes, while preserving some smoothness (minimizing of
    *dispcurve_penaltyfunc*).
    The curve can be furthered optimized using a minimization
    algorithm, which then seek the curve that really minimizes
    the penalty function -- but does not necessarily ride through
    the local maxima any more.

    If an initial vel array is given (*varray_init*) and
    *optimizecurve*=True then only the optimization algorithm
    is applied, using *varray_init* as starting point.

    *strength_smoothing* controls the relative strength of the
    smoothing term in the penalty function.

    amplmatrix[i, j] = amplitude at period nb i and velocity nb j

    @type amplmatrix: L{numpy.ndarray}
    @type velocities: L{numpy.ndarray}
    @type varray_init: L{numpy.ndarray}
    @rtype: L{numpy.ndarray}
    """

    if not varray_init is None and optimizecurve:
        # if an initial guess for vg array is given, we simply apply
        # the optimization procedure using it as starting guess
        return optimize_dispcurve(amplmatrix=amplmatrix,
                                  velocities=velocities,
                                  vg0=varray_init,
                                  strength_smoothing=strength_smoothing)[0]

    nperiods = amplmatrix.shape[0]

    # building list of possible (v, ampl) curves at all periods
    v_ampl_arrays = None
    for iperiod in range(nperiods):
        # local maxima of amplitude at period nb *iperiod*
        argsmax = psutils.local_maxima_indices(amplmatrix[iperiod, :])

        if not argsmax:
            # no local minimum => leave nan in (v, ampl) curves
            continue

        if not v_ampl_arrays:
            # initialzing the list of possible (v, ampl) curves with local maxima
            # at current period, and nan elsewhere
            v_ampl_arrays = [(np.zeros(nperiods) * np.nan, np.zeros(nperiods) * np.nan)
                             for _ in range(len(argsmax))]
            for argmax, (varray, amplarray) in zip(argsmax, v_ampl_arrays):
                varray[iperiod] = velocities[argmax]
                amplarray[iperiod] = amplmatrix[iperiod, argmax]
            continue

        # inserting the velocities that locally maximizes amplitude
        # to the correct curves
        for argmax in argsmax:
            # velocity that locally maximizes amplitude
            v = velocities[argmax]

            # we select the (v, ampl) curve for which the jump wrt previous
            # v (not nan) is minimum
            lastv = lambda varray: varray[:iperiod][~np.isnan(varray[:iperiod])][-1]
            vjump = lambda (varray, amplarray): abs(lastv(varray) - v)
            varray, amplarray = min(v_ampl_arrays, key=vjump)

            # if the curve already has a vel attributed at this period, we
            # duplicate it
            if not np.isnan(varray[iperiod]):
                varray, amplarray = copy.copy(varray), copy.copy(amplarray)
                v_ampl_arrays.append((varray, amplarray))

            # inserting (vg, ampl) at current period to the selected curve
            varray[iperiod] = v
            amplarray[iperiod] = amplmatrix[iperiod, argmax]

        # filling curves without (vg, ampl) data at the current period
        unfilledcurves = [(varray, amplarray) for varray, amplarray in v_ampl_arrays
                          if np.isnan(varray[iperiod])]
        for varray, amplarray in unfilledcurves:
            # inserting vel (which locally maximizes amplitude) for which
            # the jump wrt the previous (not nan) v of the curve is minimum
            lastv = varray[:iperiod][~np.isnan(varray[:iperiod])][-1]
            vjump = lambda arg: abs(lastv - velocities[arg])
            argmax = min(argsmax, key=vjump)
            varray[iperiod] = velocities[argmax]
            amplarray[iperiod] = amplmatrix[iperiod, argmax]

    # amongst possible vg curves, we select the one that maximizes amplitude,
    # while preserving some smoothness
    def funcmin((varray, amplarray)):
        if not periodmask is None:
            return dispcurve_penaltyfunc(varray[periodmask],
                                         amplarray[periodmask],
                                         strength_smoothing=strength_smoothing)
        else:
            return dispcurve_penaltyfunc(varray, amplarray,
                                         strength_smoothing=strength_smoothing)
    varray, _ = min(v_ampl_arrays, key=funcmin)

    # filling holes of vg curve
    masknan = np.isnan(varray)
    if masknan.any():
        varray[masknan] = np.interp(x=masknan.nonzero()[0],
                                    xp=(~masknan).nonzero()[0],
                                    fp=varray[~masknan])

    # further optimizing curve using a minimization algorithm
    if optimizecurve:
        # first trying with initial guess = the one above
        varray1, funcmin1 = optimize_dispcurve(amplmatrix=amplmatrix,
                                               velocities=velocities,
                                               vg0=varray,
                                               periodmask=periodmask,
                                               strength_smoothing=strength_smoothing)
        # then trying with initial guess = constant velocity 3 km/s
        varray2, funcmin2 = optimize_dispcurve(amplmatrix=amplmatrix,
                                               velocities=velocities,
                                               vg0=3.0 * np.ones(nperiods),
                                               periodmask=periodmask,
                                               strength_smoothing=strength_smoothing)
        varray = varray1 if funcmin1 <= funcmin2 else varray2

    return varray


def optimize_dispcurve(amplmatrix, velocities, vg0, periodmask=None,
                       strength_smoothing=STRENGTH_SMOOTHING):
    """
    Optimizing vel curve, i.e., looking for curve that really
    minimizes *dispcurve_penaltyfunc* -- and does not necessarily
    ride any more through local maxima

    Returns optimized vel curve and the corresponding
    value of the objective function to minimize

    @type amplmatrix: L{numpy.ndarray}
    @type velocities: L{numpy.ndarray}
    @rtype: L{numpy.ndarray}, float
    """
    if np.any(np.isnan(vg0)):
        raise Exception("Init velocity array cannot contain NaN")

    nperiods = amplmatrix.shape[0]

    # function that returns the amplitude curve
    # a given input vel curve goes through
    ixperiods = np.arange(nperiods)
    amplcurvefunc2d = RectBivariateSpline(ixperiods, velocities, amplmatrix, kx=1, ky=1)
    amplcurvefunc = lambda vgcurve: amplcurvefunc2d.ev(ixperiods, vgcurve)

    def funcmin(varray):
        """Objective function to minimize"""
        # amplitude curve corresponding to vel curve
        if not periodmask is None:
            return dispcurve_penaltyfunc(varray[periodmask],
                                         amplcurvefunc(varray)[periodmask],
                                         strength_smoothing=strength_smoothing)
        else:
            return dispcurve_penaltyfunc(varray,
                                         amplcurvefunc(varray),
                                         strength_smoothing=strength_smoothing)
            
    bounds = nperiods * [(min(velocities) + 0.1, max(velocities) - 0.1)]
    method = 'SLSQP'  # methods with bounds: L-BFGS-B, TNC, SLSQP
    resmin = minimize(fun=funcmin, x0=vg0, method=method, bounds=bounds)
    vgcurve = resmin['x']
    # _ = funcmin(vgcurve, verbose=True)

    return vgcurve, resmin['fun']


def dispcurve_penaltyfunc(vgarray, amplarray, strength_smoothing=STRENGTH_SMOOTHING):
    """
    Objective function that the vg dispersion curve must minimize.
    The function is composed of two terms:

    - the first term, - sum(amplitude), seeks to maximize the amplitudes
      traversed by the curve
    - the second term, sum(dvg**2) (with dvg the difference between
      consecutive velocities), is a smoothing term penalizing
      discontinuities

    *vgarray* is the velocity curve function of period, *amplarray*
    gives the amplitudes traversed by the curve and *strength_smoothing*
    is the strength of the smoothing term.

    @type vgarray: L{numpy.ndarray}
    @type amplarray: L{numpy.ndarray}
    """
    # removing nans
    notnan = ~(np.isnan(vgarray) | np.isnan(amplarray))
    vgarray = vgarray[notnan]

    # jumps
    dvg = vgarray[1:] - vgarray[:-1]
    sumdvg2 = np.sum(dvg**2)

    # amplitude
    sumamplitude = amplarray.sum()

    # vg curve must maximize amplitude and minimize jumps
    return -sumamplitude + strength_smoothing*sumdvg2


if __name__ == '__main__':
    # loading pickled cross-correlations
    xc = load_pickled_xcorr_interactive()
    print "Cross-correlations available in variable 'xc':"
    print xc
