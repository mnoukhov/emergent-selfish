#!/bin/bash
#SBATCH --account=def-bengioy
#SBATCH --array=1-15
#SBATCH --cpus-per-task=1
#SBATCH --output=/home/noukhovm/scratch/slurm-logs/hyperparam-search.%A.%a.out
#SBATCH --error=/home/noukhovm/scratch/slurm-logs/hyperparam-search.%A.%a.err
#SBATCH --job-name=emergent-hyperparam
#SBATCH --mem=4GB
#SBATCH --time=2:59:00

module load python/3.7
module load scipy-stack
virtualenv --no-download $SLURM_TMPDIR/env
source $SLURM_TMPDIR/env/bin/activate
pip install --no-index --upgrade pip
pip install --no-index -r requirements.txt
pip install -e .

experiment_name="deter-deter-bias6-search"

orion hunt -n $experiment_name	\
	--working-dir $SLURM_TMPDIR/$experiment_name \
	--max-trials 150
	src/orion_runs.py --config configs/deter-deter-search.gin --savedir {trial.working_dir}

cp -r $SLURM_TMPDIR/$exp_name $SCRATCH/emergent-selfish/$exp_name
