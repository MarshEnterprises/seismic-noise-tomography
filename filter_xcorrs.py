#!/usr/bin/env python
from pysismo.pscrosscorr import CrossCorrelationCollection


def filter_by_distance(xc, dmax=None, dmin=None):
    xcr = CrossCorrelationCollection()
    for s1 in xc:
        for s2 in xc[s1]:
            if xc[s1][s2] > dmin and xc[s1][s2] < dmax:
                xcr[s1] = {s2: xc[s1][s2]}
    return xcr

