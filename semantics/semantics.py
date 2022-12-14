import sys
import random
from collections import defaultdict, Counter 
import json
import math
import os
import datetime
import numpy as np
import torch

sys.setrecursionlimit(int(1e4))
sys.path.insert(0, "./semantics/dreamcoder")
from dreamcoder.dreamcoder import commandlineArguments, explorationCompression
from dreamcoder.utilities import eprint, flatten, testTrainSplit, numberOfCPUs
from dreamcoder.grammar import Grammar
from dreamcoder.task import Task
from dreamcoder.type import Context, arrow, tbool, tlist, tint, t0, UnificationFailure
from dreamcoder.recognition import RecurrentFeatureExtractor
from dreamcoder.program import Program, Invented, Primitive
from dreamcoder.frontier import Frontier, FrontierEntry
from dreamcoder.domains.hint.main import list_options, LearnedFeatureExtractor
from bin import hint

from datasets import EMPTY_VALUE, MISSING_VALUE

class ProgramWrapper(object):
    def __init__(self, prog):
        try:
            self.fn = prog.evaluate([])
        except RecursionError as e:
            self.fn = None
        self.prog = prog
        self.arity = len(prog.infer().functionArguments())
        self._name = None
        self.cache = {} # used for fast computation
    
    def __call__(self, inputs):
        if len(inputs) != self.arity or MISSING_VALUE in inputs:
            return MISSING_VALUE
        if inputs in self.cache:
            return self.cache[inputs]
        try:
            fn = self.fn
            for x in inputs:
                if isinstance(x, tuple):
                    x = list(x) # dreamcoder accepts list as input and output, but we want tuple
                fn = fn(x)
            if isinstance(fn, list):
                fn = tuple(fn)
        except (TypeError, RecursionError, IndexError) as e:
            print(repr(e))
            fn = MISSING_VALUE
        self.cache[inputs] = fn
        return fn

    def __eq__(self, prog): # only used for removing equivalent semantics
        if self.arity != prog.arity:
            return False
        if isinstance(self.fn, int) and isinstance(prog.fn, int):
            return self.fn == prog.fn
        # if self.y is not None and prog.y is not None:
        #     assert len(self.y) == len(prog.y) # the program should be evaluated on same examples
        #     return np.mean(self.y[self.y!=None] == prog.y[self.y!=None]) > 0.95
        if str(self.prog) != 'GT':
            return str(self.prog) == str(prog.prog)
        return False

    def __str__(self):
        return "%s %s"%(self.name, self.prog)

    @property
    def name(self):
        if self._name is not None: return self._name
        if isinstance(self.fn, int):
            self._name = str(self.fn)
        else:
            self._name = "fn"
            pass # TODO: assign name based on the function
        return self._name

    def evaluate(self, examples, store_y=True): 
        ys = []
        for exp in examples:
            y = self(exp)
            ys.append(y)
        return ys

class NULLProgram(object):
    def __init__(self):
        self.arity = 0
        self.fn = lambda: EMPTY_VALUE
        self.cache = {}
        self.prog = 'NULL'

    def __call__(self, inputs):
        if len(inputs) != 0:
            return MISSING_VALUE
        return self.fn()
    
    def evaluate(self, examples, **kwargs): 
        ys = [self(e) for e in examples]
        return ys

    def __str__(self):
        return str(self.prog)

    def solve(self, *args):
        return []

def compute_likelihood(program, examples=None, weighted_likelihood=False):
    if not examples:
        return 0., None
    else:
        pred = program.evaluate([e[0] for e in examples], store_y=False)
        gt = [e[1] for e in examples]
        res = [x == y for x, y in zip(pred, gt)]
        likehood = np.mean(res) 
        if weighted_likelihood:
            # adjust likelihood based on the number of examples and the arity
            coef = max(1, 10 * program.arity / len(examples))
            likehood /= coef
        return likehood, np.array(res)

class Semantics(object):
    def __init__(self, idx, arity, program, gt_program=None, fewshot=False, learnable=True):
        self.idx = idx
        self.examples = []
        self.program = program
        self.gt_program = gt_program
        self.arity = arity
        self.solved = False
        self.fewshot = fewshot
        self.learnable = learnable
        self.likelihood = 0. if self.learnable else 1.
        self.cache = {}

    def update_examples(self, examples):
        examples = [x[:2] for x in examples if len(x[0]) == self.arity] 
        for x, ys in self.cache.items():
            for y in ys:
                ys[y] *= 0.5
        
        for x, y in examples:
            if x not in self.cache:
                self.cache[x] = {}
            if y not in self.cache[x]:
                self.cache[x][y] = 0.
            self.cache[x][y] += 1

        
        conf_examples = []
        conf_thres = 1
        for x, ys in self.cache.items():
            y, conf = max(ys.items(), key=lambda k: k[1])
            if conf >= conf_thres and (conf / sum(ys.values())) > 0.5: # the most confident y occupies 80% of all possible prediction
                conf_examples.append((x, y))
        self.examples = conf_examples

        self.likelihood, self.res = compute_likelihood(self.program, conf_examples, weighted_likelihood=True)
        self.check_solved()

        acc = compute_likelihood(self.gt_program, examples)[0]
        acc_conf = compute_likelihood(self.gt_program, conf_examples)[0]
        print(f"Symbol-{self.idx:02d}: arity: {self.arity}, examples (conf): {len(examples)} ({len(conf_examples)}), accuracy (conf): {acc*100:.2f} ({acc_conf*100:.2f})")

    def update_program(self, entry):
        program = ProgramWrapper(entry.program)
        likelihood = compute_likelihood(program, self.examples, weighted_likelihood=True)[0]
        if (likelihood > self.likelihood) or \
            (likelihood == self.likelihood and len(str(program)) < len(str(self.program))):
            self.program = program
            self.likelihood = likelihood
            self.check_solved()
    
    def check_solved(self):
        if self.program is None:
            self.solved = False
        elif self.likelihood >= 0.95:
            self.solved = True
        elif self.fewshot and self.likelihood >= 0.95 and len(set(self.examples)) >= 10:
            self.solved = True
        else:
            self.solved = False

    def __call__(self, inputs):
        inputs = tuple([x for x in inputs if x != EMPTY_VALUE])
        if self.likelihood > 0.5:
            return self.program(inputs)
        elif inputs in self.cache:
            ys = self.cache[inputs]
            candidates = [y[0] for y in ys.items()]
            p = [y[1] for y in ys.items()]
            p = np.array(p) / sum(p)
            id = int(np.random.choice(len(p), p=p))
            output = candidates[id]
            return output

        return MISSING_VALUE

    def make_task(self):
        min_examples = 1
        max_examples = 100
        examples = self.examples
        if len(examples) < min_examples or self.solved or not self.learnable:
            return None
        data_type = type(examples[0][1])
        assert data_type in [int, tuple, list], "Unknown data type."
        data_type = tint if data_type is int else tlist(tint)
        task_type = arrow(*([data_type]*(self.arity + 1)))
        if len(examples) > max_examples:
            wrong_examples = [e for e, r in zip(examples, self.res) if not r]
            right_examples = [e for e, r in zip(examples, self.res) if r]
            right_examples = random.choices(right_examples, k=max_examples-len(wrong_examples))
            examples = wrong_examples + right_examples
            examples = random.sample(examples, k=max_examples)
        return Task(str(self.idx), task_type, examples)

    def solve(self, i, inputs, output_list):
        if len(inputs) != self.arity:
            return []

        def equal(a, b, pos):
            if len(a) != len(b):
                return False
            for j in range(len(a)):
                if j == pos:
                    continue
                if a[j] != b[j]:
                    return False
            return True

        candidates = []
        for xs, y in self.cache.items():
            y = sorted(y.items(), key = lambda p: p[1], reverse=True)[0][0]
            if y in output_list and equal(xs, inputs, i):
                candidates.append(xs[i])
        
        # test if we can reach the target by make it an empty list or empty value
        inputs = inputs[:]
        data_type = type(output_list[0])
        if data_type is int:
            empty = EMPTY_VALUE
            inputs = inputs[:i] + inputs[i+1:]
        else:
            empty = ()
            inputs[i] = empty
        if self(inputs) in output_list:
            candidates.append(empty)

        return candidates

    def clear(self):
        self.examples = []
        self.program = NULLProgram()
        self.solved = False
        self.likelihood = 0.
        self.cache = {}
    
    def save(self):
        model = {'idx': self.idx, 'solved': self.solved, 'likelihood': self.likelihood, 'arity': self.arity}
        model['program'] = None if isinstance(self.program, NULLProgram) else self.program.prog
        return model

    def load(self, model):
        self.idx = model['idx']
        self.solved = model['solved']
        self.likelihood = model['likelihood']
        self.arity = model['arity']
        if self.learnable:
            self.program = ProgramWrapper(model['program'])

class DreamCoder(object):
    def __init__(self, config=None):
        self.config = config
        domain = config.domain

        args = commandlineArguments(
            enumerationTimeout=200, activation='tanh', iterations=1, recognitionTimeout=3600,
            a=3, maximumFrontier=5, topK=2, pseudoCounts=30.0,
            helmholtzRatio=0.5, structurePenalty=1.,
            CPUs=min(numberOfCPUs(), 8),
            extras=list_options)

        args['noConsolidation'] = True
        args.pop("random_seed")
        args['contextual'] = True
        args['biasOptimal'] = True
        args['auxiliaryLoss'] = True
        args['activation'] = "relu"
        args['useDSL'] = False


        extractor = {
            "learned": LearnedFeatureExtractor,
        }[args.pop("extractor")]
        extractor.H = args.pop("hidden")

        timestamp = datetime.datetime.now().isoformat()
        outputDirectory = "tmp/%s"%timestamp
        os.system("mkdir -p %s"%outputDirectory)
        
        args.update({
            "featureExtractor": extractor,
            "outputPrefix": "%s/hint"%outputDirectory,
            "evaluationTimeout": 0.0005,
        })
        args.pop("maxTasks")
        args.pop("split")
        
        import importlib
        self.primitives = importlib.import_module(f'bin.{domain.name}').primitives
        if not config.Y_combinator:
            self.primitives = [x for x in self.primitives if 'fix' not in x.name]
        baseGrammar = Grammar.uniform(self.primitives)
        self.grammar = baseGrammar
        self.train_args = args
        self.semantics = []
        for i, s in enumerate(domain.i2w):
            arity = domain.sym2arity[s]
            gt_prog = domain.sym2prog[s]
            learnable = domain.sym2learnable[s]
            smt = Semantics(i, arity, learnable=learnable, program=NULLProgram() if learnable else gt_prog, gt_program=gt_prog)
            self.semantics.append(smt)
        self.allFrontiers = None
        self.helmholtzFrontiers = None
        self.learn_count = 0

    def __call__(self):
        return self.semantics

    def save(self):
        model = [smt.save() for smt in self.semantics]
        return model

    def load(self, model):
        if model is None:
            return
        assert len(self.semantics) == len(model)
        for i in range(len(self.semantics)):
            self.semantics[i].load(model[i])
    
    def extend(self, n):
        for smt in self.semantics:
            smt.learnable = False
        idx = len(self.config.domain.i2w) - 1
        self.semantics.append(Semantics(idx, fewshot=True))
        self.primitives.extend([Invented(smt.program.prog) for smt in self.semantics if not smt.learnable and smt.arity > 0])

    def rescore_frontiers(self, tasks):
        if self.allFrontiers is None:
            return
        print('Rescoring %d frontiers...'%len(self.allFrontiers))
        id2task = {t.name: t for t in tasks}
        id2frontier = {f.task.name: f for f in self.allFrontiers}
        allFrontiers = {}
        for name in id2task.keys():
            task = id2task[name]
            examples = task.examples
            if name not in id2frontier:
                frontier = Frontier([], task=task)
            else:
                frontier = id2frontier[name]
                frontier.task = task
                for entry in frontier.entries:
                    program = ProgramWrapper(entry.program)
                    entry.logLikelihood = float(np.log(compute_likelihood(program=program, examples=examples)[0]))
                    entry.logPosterior = entry.logLikelihood + entry.logPrior
                frontier.removeLowLikelihood(low=0.1)

            allFrontiers[task] = frontier
        self.allFrontiers = allFrontiers

    def learn(self, dataset):
        self.learn_count += 1
        learning_interval = 5
        tasks = []
        max_arity = 0
        for smt, exps in zip(self.semantics, dataset):
            if not smt.learnable:
                continue
            smt.update_examples(exps)
            if self.learn_count % learning_interval == 0:
                t = smt.make_task()
                if t is not None:
                    tasks.append(t)
                    max_arity = max(smt.arity, max_arity)
        if not tasks: 
            return
        self.train_args['enumerationTimeout'] = 5 if max_arity == 0 else 300
        # self.train_args['iterations'] = 1 if max_arity == 0 else 3
        n_solved = len(['' for t in self.semantics if t.solved or not t.learnable])
        print("Semantics: %d/%d/%d (total/solved/learn)."%(len(self.semantics), n_solved, len(tasks)))
        if len(tasks) == 0:
            self._print_semantics()
            return 
        self._print_tasks(tasks)
        if getattr(self.config.domain, "update_grammar", False):
            self.update_grammar()
        print(self.grammar)
        # print(self.allFrontiers)
        # self.rescore_frontiers(tasks)
        # if self.allFrontiers is not None:
        #     print(self.allFrontiers.values())

        if self.helmholtzFrontiers is not None:
            requests_old ={x.task.request for x in self.helmholtzFrontiers()}
            requests = {t.request for t in tasks}
            # if new requests, discard old helmholtz frontiers
            if requests != requests_old:
                self.helmholtzFrontiers = None

        result = explorationCompression(self.grammar, tasks, **self.train_args)
        self.allFrontiers = list(result.allFrontiers.values())
        self.helmholtzFrontiers = result.helmholtzFrontiers

        for frontier in result.taskSolutions.values():
            if not frontier.entries: continue
            symbol_idx = int(frontier.task.name)
            # print(frontier)
            self.semantics[symbol_idx].update_program(frontier.bestPosterior)
        # examples = [xs for t in tasks for xs, y in t.examples]
        # self._removeEquivalentSemantics(examples)
        self._removeEquivalentSemantics()
        self._print_semantics()
        # self.grammar = result.grammars[-1]

    def update_grammar(self):
        new_primitives = []
        for smt in self.semantics:
            if '#' in str(smt.program) or '+' in str(smt.program) or '-' in str(smt.program):
                # if '#+-' in the program, the program uses a invented primitive, it is very likely to have a high computation cost.
                # Therefore we don't add this program into primitives, since it might slow the enumeration a lot.
                # it might be resolved by increasing the enumeration time
                continue
            if smt.learnable and smt.solved and smt.arity == 2:
                add = ProgramWrapper(hint.add)
                minus0 = ProgramWrapper(hint.minus0)
                if np.all([smt.program(x) in [add(x), MISSING_VALUE] for x, y in smt.examples]):
                    new_primitives.append(hint.add)
                elif np.all([smt.program(x) in [minus0(x), MISSING_VALUE] for x, y in smt.examples]):
                    new_primitives.append(hint.minus0)
                else:
                    new_primitives.append(Invented(smt.program.prog))

        new_grammar = Grammar.uniform(self.primitives + new_primitives)
        if new_grammar != self.grammar:
            self.grammar = new_grammar
            self.helmholtzFrontiers = None
            self.allFrontiers = None
            print("Update grammar with invented programs and set frontiers to none.")
        

    def _print_semantics(self):
        for smt in sorted(self.semantics, key=lambda x: x.idx):
            print("Symbol-%02d: %s %.2f"%(smt.idx, smt.program, smt.likelihood))
            # print("Solved!" if smt.solved else "")

    def _print_tasks(self, tasks):
        for task in tasks:
            print("Symbol-%02d (%s), Samples: %3d, "%(int(task.name), task.request, len(task.examples)), task.examples[:10])

        # json.dump([t.examples for t in tasks], open('outputs/tasks.json', 'w'))

    def _removeEquivalentSemantics(self, examples=None):
        if examples is not None:
            examples = list(set(examples))
            for smt in self.semantics:
                if smt.program is not None:
                    smt.program.evaluate(examples)
        
        for i in range(len(self.semantics) - 1):
            smt_i = self.semantics[i]
            if smt_i.program is None:
                continue
            for j in range(i+1, len(self.semantics)):
                smt_j = self.semantics[j]
                if smt_j.program is None:
                    continue
                if smt_i.program == smt_j.program:
                    if len(smt_i.examples) >= len(smt_j.examples):
                        smt_j.clear()
                    else:
                        smt_i.clear()
                        break
