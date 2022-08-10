#!/usr/bin/env python

import matplotlib.pyplot as plt
import numpy as np
import pickle

class Section:

    def __init__(self, path):
        self.t = pickle.load(open(path))
        
        # get all available periods
        periods = np.array(self.t.items())[:, 0]
        periods = np.sort(periods)
        
        # get minimum and maximum grid node and check consistent interval
        xmin = self.t[periods[0]].grid.xmin
        xmax = self.t[periods[0]].grid.get_xmax()
        xstep = self.t[periods[0]].grid.xstep
       
        ymin = self.t[periods[0]].grid.ymin
        ymax = self.t[periods[0]].grid.get_ymax()
        ystep = self.t[periods[0]].grid.ystep
         
        for p in periods:
            xmin = min(xmin, self.t[p].grid.xmin)
            xmax = max(xmax, self.t[p].grid.get_xmax())
            if xstep != self.t[p].grid.xstep:
                print('inconsistent grid steps, aborted')
                sys.exit()
 
            ymin = min(ymin, self.t[p].grid.ymin)
            ymax = max(ymax, self.t[p].grid.get_ymax())
            if ystep != self.t[p].grid.ystep:
                print('inconsistent grid sizes, aborted')
                sys.exit()          
        
        self.xmin = xmin
        self.xmax = xmax 
        self.xstep = xstep

        self.ymin = ymin
        self.ymax = ymax
        self.ystep = ystep
       
        self.periods = periods

        print('x axis: ', xmin, xmax, xstep)
        print('y axis: ', ymin, ymax, ystep)

        # now stack velocity maps into ndarray
        
        all_longs = np.linspace(xmin, xmax, (xmax-xmin)/xstep+1)
        all_lats = np.linspace(ymin, ymax, (ymax-ymin)/ystep+1)
        
       # v_nan = []

       # for p in periods:
       #     x_present = self.t[p].grid.xy_nodes()[0]
       #     y_present = self.t[p].grid.xy_nodes()[1]
       #     v_present = self.t[p].v0 / (1 + self.t[p].mopt)
       #     
       #     v_current_p = np.ones(len(all_longs)*len(all_lats)) * np.nan
       #     for i in all_longs:
       #         for j in all_lats:
       #             print(i, j)
       #         pass
       #             
                        
            

            #print(x_present, y_present, v_present)
        print(all_lats)

        

    def plot_long_slice(self, n_long):
        section = []
        #periods = np.linspace(1, 5, int(4/0.025 + 1))
        #periods = np.linspace(0.14, 1, ((1-0.14)/0.005)+1)
        #periods = np.array(self.t.items())[:, 0]
        #periods = np.sort(periods)
        periods = self.periods
        periods = periods[periods >= 0.745]

        for p in periods:
            #p = round(p, 4) # prevents floating point errors
            section = section + [self.t[p].grid.to_2D_array(self.t[p].v0 / (1 + self.t[p].mopt)).T[:, n_long]]
            #print(p, len(self.t[p].grid.to_2D_array(self.t[p].v0 / (1 + self.t[p].mopt)).T[:, n_long]))
            #print(p)

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
        
#sec01_1 = Section('../output/tomography/2-pass-tomography_2015-2015_xmlresponse_LG_d7_f0.1-1_e1-45_all_pairs_test_0.1-1.0.pickle')
#sec1_5 = Section('../output/tomography/2-pass-tomography_2015-2015_xmlresponse_LG_d7_f1-5_e1-45___section.pickle')

#sec01_1.plot_long_slice(5)
#sec1_5.plot_long_slice(5)


