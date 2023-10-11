""""
Module defining the main Particle Swarm optimizer class.
"""
import time
import json
import numpy as np
import logging
import multiprocessing as mp
from pathlib import Path

import mols2grid
import pandas as pd
from loguru import logger
from rdkit import Chem, rdBase
from tqdm import tqdm

from mso.swarm import Swarm
from mso.util import canonicalize_smiles
rdBase.DisableLog('rdApp.error')
logging.getLogger('tensorflow').disabled = True


class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super(NumpyEncoder, self).default(obj)
    

class BasePSOptimizer:
    """
        Base particle swarm optimizer class. It handles the optimization of a swarm object.
    """
    def __init__(self, swarms, inference_model, scoring_functions=None, init_smiles_set: set[str] = set()):
        """

        :param swarms: List of swarm objects each defining an individual particle swarm that
            is used for optimization.
        :param inference_model: The inference model used to encode/decode smiles to/from the
            Continuous Data-Diven molecular Descriptor (CDDD) space.
        :param scoring_functions: List of functions that are used to evaluate a generated molecule.
            Either take a RDKit mol object as input or a point in the cddd space.
        """
        self.infer_model = inference_model
        self.scoring_functions = scoring_functions
        self.swarms = swarms
        self.best_solutions = pd.DataFrame(columns=["smiles", "fitness"])
        self.best_fitness_history = pd.DataFrame(columns=["step", "swarm", "fitness"])

        self.smi_to_unscaled_scores: dict[str, float] = {}
        self.smi_to_scaled_scores: dict[str, float] = {}
        self.smi_to_desirability_scores: dict[str, float] = {}
        self.smi_to_residues: dict[str, str] = {}
        self.init_smiles_set = {canonicalize_smiles(smi) for smi in init_smiles_set}

    def update_fitness(self, swarm):
        """
        Method that calculates and updates the fitness of each particle in  a given swarm. A
        particles fitness is defined as weighted average of each scoring functions output for
        this particle.
        :param swarm: The swarm that is updated.
        :return: The swarm that is updated.
        """
        assert self.scoring_functions is not None
        weight_sum = 0
        fitness = 0

        swarm.smiles = [canonicalize_smiles(smi) for smi in swarm.smiles]

        uniq_smis = [smi for smi in set(swarm.smiles) if smi not in self.smi_to_unscaled_scores]
        mol_list = [Chem.MolFromSmiles(sml) for sml in uniq_smis]
        for scoring_function in self.scoring_functions:
            if scoring_function.is_mol_func:
                unscaled_scores, scaled_scores, desirability_scores, residues = scoring_function(mol_list)
                for smi, unscaled_score, scaled_score, desirability_score, residue in zip(
                    uniq_smis, unscaled_scores, scaled_scores, desirability_scores, residues
                ):
                    self.smi_to_unscaled_scores[smi] = unscaled_score
                    self.smi_to_scaled_scores[smi] = scaled_score
                    self.smi_to_desirability_scores[smi] = desirability_score
                    self.smi_to_residues[smi] = residue  # for viz only, not used for optimisation
                # remap to full list of scores
                unscaled_scores = np.array([self.smi_to_unscaled_scores[smi] for smi in swarm.smiles])
                scaled_scores = np.array([self.smi_to_scaled_scores[smi] for smi in swarm.smiles])
                desirability_scores = np.array([self.smi_to_desirability_scores[smi] for smi in swarm.smiles])
            else:
                unscaled_scores, scaled_scores, desirability_scores, _ = scoring_function(swarm.x)

            swarm.unscaled_scores[scoring_function.name] = unscaled_scores
            swarm.scaled_scores[scoring_function.name] = scaled_scores
            swarm.desirability_scores[scoring_function.name] = desirability_scores
            fitness += scaled_scores
            weight_sum += scoring_function.weight
        fitness /= weight_sum
        swarm.update_fitness(fitness)
        return swarm

    def _next_step_and_evaluate(self, swarm):
        """
        Method that wraps the update of the particles position (next step) and the evaluation of
        the fitness at these new positions.
        :param swarm: The swarm that is updated.
        :return: The swarm that is updated.
        """
        swarm.next_step()
        smiles = self.infer_model.emb_to_seq(swarm.x)
        swarm.smiles = smiles
        swarm.x = self.infer_model.seq_to_emb(swarm.smiles)
        swarm = self.update_fitness(swarm)
        return swarm

    def _update_best_solutions(self, num_track):
        """
        Method that updates the best_solutions dataframe that keeps track of the overall best
        solutions over the course of the optimization.
        :param num_track: Length of the best_solutions dataframe.
        :return: The max, min and mean fitness of the best_solutions dataframe.
        """
        new_df = pd.DataFrame(columns=["smiles", "fitness", "residues"])
        new_df.smiles = [sml for swarm in self.swarms for sml in swarm.smiles]
        new_df.fitness = [fit for swarm in self.swarms for fit in swarm.fitness]
        new_df.residues = [self.smi_to_residues[smi] for smi in new_df.smiles]

        # NOTE: remove SMILES which match the SMILES used to initialise the particle swarms
        new_df = new_df[~new_df.smiles.isin(self.init_smiles_set)]

        self.best_solutions = pd.concat([self.best_solutions, new_df])
        self.best_solutions = self.best_solutions.drop_duplicates("smiles")
        self.best_solutions = self.best_solutions.sort_values("fitness", ascending=False).reset_index(drop=True)
        self.best_solutions = self.best_solutions.iloc[:num_track]

        best_solutions_max = self.best_solutions.fitness.max()
        best_solutions_min = self.best_solutions.fitness.min()
        best_solutions_mean = self.best_solutions.fitness.mean()
        return best_solutions_max, best_solutions_min, best_solutions_mean

    def _update_best_fitness_history(self, step):
        """
        tracks best solutions for each swarm
        :param step: The current iteration step of the optimizer.
        :return: None
        """
        new_df = pd.DataFrame(columns=["step", "swarm", "fitness", "smiles", "residues"])
        new_df.fitness = [swarm.swarm_best_fitness for swarm in self.swarms]
        new_df.smiles = [swarm.best_smiles for swarm in self.swarms]
        new_df.residues = [self.smi_to_residues[str(smi)] for smi in new_df.smiles]
        new_df.swarm = [i for i in range(len(self.swarms))]
        new_df.step = step
        # self.best_fitness_history = self.best_fitness_history.append(new_df, sort=False)
        self.best_fitness_history = pd.concat([self.best_fitness_history, new_df], ignore_index=True, sort=False)

    def run(self, num_steps: int, num_track: int = 1_000_000, out_dir: Path = Path("./results/")):
        """
        The main optimization loop.
        :param num_steps: The number of update steps.
        :param num_track: Number of best solutions to track.
        :return: The optimized particle swarm.
        """
        out_dir.mkdir(exist_ok=False, parents=True)

        # evaluate initial score
        for swarm in self.swarms:
            self.update_fitness(swarm)

        # run particle swarm optimisation
        with open(out_dir / "epoch_stats.txt", "a") as epoch_stats_file:
            for step in tqdm(range(num_steps), desc="running steps"):
                self._update_best_fitness_history(step)
                max_fitness, min_fitness, mean_fitness = self._update_best_solutions(num_track)
                epoch_stats = f"Step {step:d}, max: {max_fitness:.3f}, min: {min_fitness:.3f}, mean: {mean_fitness:.3f}"
                logger.success(epoch_stats)
                epoch_stats_file.write(epoch_stats + "\n")
                
                for swarm in tqdm(self.swarms, desc="updating each swarm"):
                    self._next_step_and_evaluate(swarm)
                            
                # save
                self.best_solutions.to_csv(out_dir / "best_solutions.csv", index=False)
                self.best_fitness_history.to_csv(out_dir / "best_fitness_history.csv", index=False)
                mols2grid.save(
                    self.best_solutions, 
                    smiles_col="smiles", 
                    output=out_dir / "generated_smiles_and_fitnesses_grid.html", 
                    subset=["fitness", "residues"]
                )
                with open(out_dir / "smi_to_unscaled_scores.json", "w") as f:
                    # use this to speed up a future run, assuming oracle params like residues_of_interest are identical
                    json.dump(self.smi_to_unscaled_scores, f, cls=NumpyEncoder)
        return self.swarms

    @classmethod
    def from_query(cls, init_smiles, num_part, num_swarms, inference_model,
                   scoring_functions=None, phi1=2., phi2=2., phi3=2., x_min=-1.,
                   x_max=1., v_min=-0.6, v_max=0.6, **kwargs):
        """
        Classmethod to create a PSO instance with (possible) multiple swarms which particles are
        initialized at the position of the embedded input SMILES. All swarms are initialized at the
        same position.
        :param init_smiles: (string) The SMILES the defines the molecules which acts as starting
            point of the optimization. If it is a list of multiple smiles, num_part smiles will be randomly drawn.
        :param num_part: Number of particles in each swarm.
        :param num_swarms: Number of individual swarm to be optimized.
        :param inference_model: A inference model instance that is used for encoding an decoding
            SMILES to and from the CDDD space.
        :param scoring_functions: List of functions that are used to evaluate a generated molecule.
            Either take a RDKit mol object as input or a point in the cddd space.
        :param phi1: PSO hyperparamter.
        :param phi2: PSO hyperparamter.
        :param phi3: PSO hyperparamter.
        :param x_min: min bound of the optimization space (should be set to -1 as its the default
            CDDD embeddings take values between -1 and 1).
        :param x_max: max bound of the optimization space (should be set to -1 as its the default
            CDDD embeddings take values between -1 and 1).
        :param v_min: minimal velocity component of a particle. Also used as lower bound for the
            uniform distribution used to sample the initial velocity.
        :param v_max: maximal velocity component of a particle. Also used as upper bound for the
            uniform distribution used to sample the initial velocity.
        :param kwargs: additional parameters for the PSO class
        :return: A PSOptimizer instance.
        """
        embedding = inference_model.seq_to_emb(init_smiles)
        swarms = [
            Swarm.from_query(
                init_sml=init_smiles,
                init_emb=embedding,
                num_part=num_part,
                v_min=v_min,
                v_max=v_max,
                x_min=x_min,
                x_max=x_max,
                phi1=phi1,
                phi2=phi2,
                phi3=phi3
            ) for _ in range(num_swarms)
        ]
        return cls(swarms, inference_model, scoring_functions, set(init_smiles), **kwargs)

    @classmethod
    def from_query_list(cls, init_smiles, num_part, num_swarms, inference_model,
                        scoring_functions=None, phi1=2., phi2=2., phi3=2., x_min=-1.,
                        x_max=1., v_min=-0.6, v_max=0.6, **kwargs):
        """
        Classmethod to create a PSO instance with (possible) multiple swarms which particles are
        initialized at the position of the embedded input SMILES. Each swarms is  initialized at
        the position defined by the different SMILES in the input list.
        :param init_smiles: A List of SMILES which each define the molecule which acts as starting
            point of each swarm in the optimization.
        :param num_part: Number of particles in each swarm.
        :param num_swarms: Number of individual swarm to be optimized.
        :param inference_model: A inference model instance that is used for encoding an decoding
            SMILES to and from the CDDD space.
        :param scoring_functions: List of functions that are used to evaluate a generated molecule.
            Either take a RDKit mol object as input or a point in the cddd space.
        :param phi1: PSO hyperparamter.
        :param phi2: PSO hyperparamter.
        :param phi3: PSO hyperparamter.
        :param x_min: min bound of the optimization space (should be set to -1 as its the default
            CDDD embeddings take values between -1 and 1).
        :param x_max: max bound of the optimization space (should be set to -1 as its the default
            CDDD embeddings take values between -1 and 1).
        :param v_min: minimal velocity component of a particle. Also used as lower bound for the
            uniform distribution used to sample the initial velocity.
        :param v_max: maximal velocity component of a particle. Also used as upper bound for the
            uniform distribution used to sample the initial velocity.
        :param kwargs: additional parameters for the PSO class
        :return: A PSOptimizer instance.
        """
        assert isinstance(init_smiles, list)
        assert len(init_smiles) == num_swarms

        embedding = inference_model.seq_to_emb(init_smiles)
        swarms = []
        for i, sml in enumerate(init_smiles):
            swarms.append(Swarm.from_query(
                init_sml=sml,
                init_emb=embedding[i],
                num_part=num_part,
                v_min=v_min,
                v_max=v_max,
                x_min=x_min,
                x_max=x_max,
                phi1=phi1,
                phi2=phi2,
                phi3=phi3))

        return cls(swarms, inference_model, scoring_functions, set(init_smiles), **kwargs)

    @classmethod
    def from_swarm_dicts(cls, swarm_dicts, inference_model, scoring_functions=None, x_min=-1., x_max=1.,
                         inertia_weight=0.9, phi1=2., phi2=2., phi3=2., **kwargs):
        """
        Classmethod to create a PSO instance from a list of dictionaries each defining an
        individual swarm.
        :param swarm_dicts: A list of dictionaries each defining an individual swarm.
            See Swarm.from_dict for more info.
        :param inference_model: A inference model instance that is used for encoding an decoding
            SMILES to and from the CDDD space.
        :param scoring_functions: List of functions that are used to evaluate a generated molecule.
            Either take a RDKit mol object as input or a point in the cddd space.
        :param kwargs: additional parameters for the PSO class
        :return: A PSOptimizer instance.
        """
        swarms = [Swarm.from_dict(
            dictionary=swarm_dict,
            x_min=x_min,
            x_max=x_max,
            inertia_weight=inertia_weight,
            phi1=phi1,
            phi2=phi2,
            phi3=phi3
        ) for swarm_dict in swarm_dicts]
        return cls(swarms, inference_model, scoring_functions, **kwargs)

    def __getstate__(self):
        """dont pickle all swarms --> faster serialization/multiprocessing"""
        return {k: v for k, v in self.__dict__.items() if k not in ('swarms',)}


class ParallelSwarmOptimizer(BasePSOptimizer):
    def _next_step_and_evaluate(self):
        """
        Method that wraps the update of the particles position (next step) and the evaluation of
        the fitness at these new positions.
        :param swarm: The swarm that is updated.
        :return: The swarm that is updated.
        """
        num_part = self.swarms[0].num_part
        emb = []
        for swarm in self.swarms:
            swarm.next_step()
            emb.append(swarm.x)
        emb = np.concatenate(emb)
        smiles = self.infer_model.emb_to_seq(emb)
        x = self.infer_model.seq_to_emb(smiles)
        for i, swarm in enumerate(self.swarms):
            swarm.smiles = smiles[i*num_part: (i+1)*num_part]
            swarm.x = x[i*num_part: (i+1)*num_part]
            swarm = self.update_fitness(swarm)

    def run(self, num_steps, num_track=10):
        """
        The main optimization loop.
        :param num_steps: The number of update steps.
        :param num_track: Number of best solutions to track.
        :return: The optimized particle swarm.
        """
        # evaluate initial score
        for swarm in self.swarms:
            self.update_fitness(swarm)
        for step in range(num_steps):
            self._update_best_fitness_history(step)
            max_fitness, min_fitness, mean_fitness = self._update_best_solutions(num_track)
            print("Step %d, max: %.3f, min: %.3f, mean: %.3f"
                  % (step, max_fitness, min_fitness, mean_fitness))
            self._next_step_and_evaluate()
        return self.swarms, self.best_solutions



class MPPSOOptimizer(BasePSOptimizer):
    """
    A PSOOptimizer class that uses multiprocessing to parallelize the optimization of multiple
    swarms. Only works if the inference_model is a instance of the inference_server class in the
    CDDD package that rolls out calculations on multiple zmq servers (possibly on multiple GPUs).
    """
    # TODO: this is different from the base class, as run() does no initial evaluation but got the evaluate query method.
    def __init__(self, swarms, inference_model, scoring_functions=None, num_workers=1):
        """
        :param swarms: List of swarm objects each defining an individual particle swarm that is
            used for optimization.
        :param inference_model: The inference model used to encode/decode smiles to/from the
            Continuous Data-Diven molecular Descriptor (CDDD) space. Should be an inference_server
            instance to benefit from multiprocessing.
        :param scoring_functions: List of functions that are used to evaluate a generated molecule.
            Either take a RDKit mol object as input or a point in the cddd space.
        :param num_workers: Number of workers used for the multiprocessing.
        """
        super().__init__(swarms, inference_model, scoring_functions)
        self.num_workers = num_workers

    def evaluate_query(self):
        pool = mp.Pool(self.num_workers)
        self.swarms = pool.map(self.update_fitness, self.swarms)
        pool.close()
        return self.swarms

    def run(self, num_steps, num_track=500):
        """
        The main optimization loop in the multiprocessing case with a bit more result
        tracking and timing.
        :param num_steps: The number of update steps.
        :param num_track: Number of best solutions to track.
        :return:
            swarms: The optimized particle swarm.
            best_solutions: The best solutions found over the course of optimization.
        """
        pool = mp.Pool(self.num_workers)
        for step in range(num_steps):
            start_time = time.time()
            self.swarms = pool.map(self._next_step_and_evaluate, self.swarms)
            end_time = time.time() - start_time
            max_fitness, min_fitness, mean_fitness = self._update_best_solutions(num_track)
            self._update_best_fitness_history(step)
            print("Step %d, max: %.3f, min: %.3f, mean: %.3f, et: %.1f s"
                  %(step, max_fitness, min_fitness, mean_fitness, end_time))
            if (num_track == 1) & (self.best_solutions[:num_track].fitness.mean() == 1.):
                break
            elif self.best_solutions[:num_track].fitness.mean() == 1.:
                break
        pool.close()
        return self.swarms, self.best_solutions

class MPPSOOptimizerManualScoring(MPPSOOptimizer):
    def __init__(self, swarms, inference_model, num_workers=1):
        super().__init__(swarms, inference_model, num_workers=num_workers)

    def _next_step_and_evaluate(self, swarm, fitness):
        """
        Method that updates the particles position (next step)
        :param swarm: The swarm that is updated.
        :return: The swarm that is updated.
        """
        swarm.update_fitness(fitness)
        swarm.next_step()
        smiles = self.infer_model.emb_to_seq(swarm.x)
        swarm.smiles = smiles
        swarm.x = self.infer_model.seq_to_emb(swarm.smiles)
        return swarm

    def run_one_iteration(self, fitness):
        pool = mp.Pool(self.num_workers)
        self.swarms = pool.starmap(self._next_step_and_evaluate, zip(self.swarms, fitness))
        pool.close()
        return self.swarms
