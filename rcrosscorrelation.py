#!/bin/sh
#SBATCH --account=geology
#SBATCH --partition=ada
#SBATCH --nodes=1 
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=40
#SBATCH --time=170:00:00
#SBATCH --job-name="tomography"
#SBATCH --mail-user=whtben003@myuct.ac.za
# # SBATCH --mail-type=BEGIN,END,FAIL
#conda activate seis2
#echo 
python -u crosscorrelation.py <<< $1 <<< $2 
