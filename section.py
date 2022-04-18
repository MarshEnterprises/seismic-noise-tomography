#!/usr/bin/env python

import matplotlib.pyplot as plt
import numpy as np
import pickle

class Section:

    def __init__(self, path):
        self.t = pickle.load(open(path))
        print('loaded')
        

    def plot_long_slice(self, n_long):
        section = []
        periods = np.linspace(1, 5, int(4/0.025 + 1))
        #periods = np.linspace(0.14, 1, ((1-0.14)/0.005)+1)
        #periods = np.array(self.t.items())[:, 0]

        for p in periods:
            p = round(p, 4) # prevents floating point errors
            section = section + [self.t[p].grid.to_2D_array(self.t[p].v0 / (1 + self.t[p].mopt)).T[:, n_long]]
            print(len(self.t[p].grid.to_2D_array(self.t[p].v0 / (1 + self.t[p].mopt)).T[:, n_long]))

        #section = np.array(section)
        
        #section = np.hstack(section)
        
        # self.section = section
        
        extent = (self.t[periods[0]].grid.ymin, self.t[periods[0]].grid.get_ymax(), max(periods), min(periods))
        
        #print(section)          
        fig, ax = plt.subplots()

        im = ax.imshow(section, aspect='auto', extent=extent, interpolation='bicubic')

        #ax.invert_yaxis()
        #ax.invert_xaxis()

        plt.colorbar(im)

        plt.show()
        
# sec = Section('../output/tomography/2-pass-tomography_2015-2015_xmlresponse_LG_d7_f0.1-1_e1-45_all_pairs_test_0.1-1.0.pickle')
#sec = Section('../output/tomography/2-pass-tomography_2015-2015_xmlresponse_LG_d7_f1-5_e1-45___section.pickle')

#sec.plot_long_slice(5)


