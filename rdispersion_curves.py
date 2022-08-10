#BATCH --account=geology
#SBATCH --partition=ada
#SBATCH --nodes=1 
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --time=72:00:00
#SBATCH --job-name="tomography"
#SBATCH --mail-user=whtben003@myuct.ac.za
# # SBATCH --mail-type=BEGIN,END,FAIL
python dispersion_curves.py
