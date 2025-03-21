import gin
import math
import re
import ray
from itertools import islice
import os
import gin
import numpy as np
import pandas as pd
import joblib
import torch
from gin import query_parameter as gin_qp
from tqdm import tqdm
from typing import Optional
from multiprocessing import Pool
from pathlib import Path
from rdkit import Chem
from rdkit.Chem import rdMolDescriptors, SpacialScore
from sklearn.preprocessing import StandardScaler
import joblib
from pathlib import Path
import numpy as np
from sklearn.preprocessing import StandardScaler

from src.path_lib import CHECKPOINT_PATH


class Tokenizer:
    def __init__(
        self,
        no_processes: int,
        dataset_size: int,
        vocab: Optional[dict[str, int]] = None,
    ):
        self.dataset_size = dataset_size
        self.no_processes = no_processes
        self.vocab = vocab

    def tokenize(self, smi: str | list[str]) -> str | list[str]:
        if isinstance(smi, list):
            return self._batch_tokenize(smi)
        elif isinstance(smi, str):
            return self._tokenize(smi)

    def encode(self, tokens: str | list[str]) -> list[int] | list[list[int]]:
        if isinstance(tokens, list):
            return self._batch_encode(tokens)
        elif isinstance(tokens, str):
            return self._encode(tokens)

    def build_vocab(self, tokens: str | list[str]) -> dict[str, int]:
        print("Building vocabulary...")
        # add special tokens
        vocab = ["[PAD]", "[CLS]", "[SEP]", "[MASK]"]
        for token in tqdm(tokens):
            token = token.split(" ")
            for t in token:
                if t not in vocab:
                    vocab.append(t)
        # add 20 [UNUSED#1] tokens to the vocab
        for i in range(20):
            vocab.append(f"[UNUSED{i}]")
        self.vocab = {token: idx for idx, token in enumerate(vocab)}
        return self.vocab

    def load_vocab(self, vocab_path: str) -> None:
        print("Loading vocabulary from {}".format(vocab_path))
        with open(vocab_path, "r") as f:
            vocab = f.readlines()
        vocab = [token.strip("\n") for token in vocab]
        self.vocab = {token: idx for idx, token in enumerate(vocab)}

    def _tokenize(self, smi: str) -> str:
        pattern = "(\[[^\]]+]|Br?|Cl?|N|O|S|P|F|I|b|c|n|o|s|p|\(|\)|\.|=|#|-|\+|\\\\|\/|:|~|@|\?|<|>|\*|\$|\%[0-9]{2}|[0-9])"  # type:ignore
        regex = re.compile(pattern)
        tokens = [token for token in regex.findall(smi)]
        assert smi == "".join(tokens)
        tokens.insert(0, "[CLS]")
        return " ".join(tokens)

    def _batch_tokenize(self, smiles: list[str]) -> list[str]:
        with Pool(processes=self.no_processes) as pool:
            tokenized = list(
                tqdm(
                    pool.imap(self._tokenize, smiles, chunksize=2 * self.no_processes),
                    total=self.dataset_size,
                )
            )
        return tokenized  # type:ignore

    def _encode(self, tokens: str) -> list[int]:
        token_list = tokens.split()
        if self.vocab is None:
            raise ValueError("Vocabulary not found. Please build vocabulary first.")
        # encode tokens
        len_vocab = len(self.vocab)
        return [
            self.vocab[token] if token in self.vocab else len_vocab
            for token in token_list
        ]

    def _batch_encode(self, tokens: list[str]) -> list[list[int]]:
        with Pool(processes=6) as pool:
            encoded = list(
                tqdm(
                    pool.imap(self._encode, tokens, chunksize=2 * self.no_processes),
                    total=self.dataset_size,
                )
            )
        return encoded


class MolFeatureExtractor:
    def __init__(self, scaler_path: Path):
        self.scaler_path = scaler_path
        self.num_workers = os.cpu_count()

    def encode(self, smi: str | list[str]) -> np.ndarray:
        if isinstance(smi, list):
            return self._batch_encode(smi)
        elif isinstance(smi, str):
            feat = MolFeatureExtractor._calculate_2D_feat(smi)  # type: ignore
            return np.expand_dims(feat, axis=0)

    @staticmethod  # type: ignore
    @ray.remote
    def _encode(smis: str) -> tuple:
        return [MolFeatureExtractor._calculate_2D_feat(smi) for smi in smis]  # type: ignore

    def _batches(self, it, chunk_size: int):
        it = iter(it)
        return iter(lambda: list(islice(it, chunk_size)), [])

    def _batch_encode(self, smiles: list[str]) -> np.ndarray:
        ray.init(num_gpus=0, log_to_driver=False)
        batch_size = 4 * 1024 * int(ray.cluster_resources()["CPU"])
        size = len(smiles)
        n_batches = size // batch_size + 1
        chunksize = int(ray.cluster_resources()["CPU"] * 16)
        features = np.zeros((size, 10), dtype=np.float32)
        i = 0
        for smis_batch in tqdm(self._batches(smiles, batch_size), total=n_batches):
            refs = [
                MolFeatureExtractor._encode.remote(mols_chunk)  # type: ignore
                for mols_chunk in self._batches(smis_batch, chunksize)
            ]
            two_d_chunks = [
                ray.get(r)
                for r in tqdm(
                    refs, desc="Calculating 2D descriptors", unit="chunk", leave=False
                )
            ]
            two_d_chunks = np.vstack(two_d_chunks)
            features[i : i + len(smis_batch)] = two_d_chunks
            i += len(smis_batch)
        ray.shutdown()
        return features

    @staticmethod
    def _calculate_2D_feat(smi):
        mol = Chem.MolFromSmiles(smi)
        sp3 = rdMolDescriptors.CalcFractionCSP3(mol)
        sps = SpacialScore.SPS(mol)
        stereo = rdMolDescriptors.CalcNumAtomStereoCenters(mol)
        rot_bonds = rdMolDescriptors.CalcNumRotatableBonds(mol)
        tpsa = rdMolDescriptors.CalcTPSA(mol)
        heterocyc = rdMolDescriptors.CalcNumHeterocycles(mol)
        no_spiro = rdMolDescriptors.CalcNumSpiroAtoms(mol)
        no_bridgehead = rdMolDescriptors.CalcNumBridgeheadAtoms(mol)
        n_macro, n_multi = MolFeatureExtractor.numMacroAndMulticycle(
            mol, mol.GetNumAtoms()
        )
        return np.array(
            [
                sp3,
                sps,
                stereo,
                rot_bonds,
                tpsa,
                heterocyc,
                no_spiro,
                no_bridgehead,
                n_macro,
                n_multi,
            ]
        )

    @staticmethod
    def numMacroAndMulticycle(mol, nAtoms):
        ri = mol.GetRingInfo()  # type: ignore
        nMacrocycles = 0
        multi_ring_atoms = {i: 0 for i in range(nAtoms)}
        for ring_atoms in ri.AtomRings():
            if len(ring_atoms) > 6:
                nMacrocycles += 1
            for atom in ring_atoms:
                multi_ring_atoms[atom] += 1
        nMultiRingAtoms = sum([v - 1 for k, v in multi_ring_atoms.items() if v > 1])
        return nMacrocycles, nMultiRingAtoms

    def standardise_features(self, features: np.ndarray) -> np.ndarray:
        if os.path.exists(self.scaler_path / f"std.bin"):
            try:
                scalar = joblib.load(self.scaler_path / f"std.bin")
                return scalar.transform(features)
            except Exception as e:
                raise (e)
        else:
            scalar = StandardScaler()
            fitted_data = scalar.fit_transform(features)
            joblib.dump(scalar, self.scaler_path / f"std.bin")
        return fitted_data


def calculate_training_steps(path_data, model_name: str) -> None:
    if model_name == "Transformer":
        batch_size = gin_qp("TFLoader.batch_size")
    elif model_name == "RoBERTa":
        batch_size = gin_qp("RoBERTaLoader.batch_size")

    acc_batches, epochs = (
        gin_qp("main.gradient_accum"),
        gin_qp("main.max_epoch"),
    )
    if not isinstance(batch_size, int):
        batch_size = gin_qp("%batch_size_tf")
    dataframe_name, splits = gin_qp("%df_name"), gin_qp("%data_split")
    df = pd.read_csv(path_data / dataframe_name)
    len_train = df.shape[0] * splits[0]
    batches_per_gpu = math.ceil(len_train / float(batch_size))
    train_steps = int(math.ceil(batches_per_gpu / acc_batches) * epochs)
    warmup_steps = int(math.ceil(batches_per_gpu / acc_batches) * 5)
    if model_name == "Transformer":
        gin.bind_parameter(
            "Transformer/torch.optim.lr_scheduler.OneCycleLR.total_steps", train_steps
        )
    elif model_name == "RoBERTa":
        gin.bind_parameter(
            "RoBERTa.configure_optimizers.num_training_steps", train_steps
        )
        gin.bind_parameter(
            "RoBERTa.configure_optimizers.num_warmup_steps", warmup_steps
        )


def load_checkpointed_gin_config(
    checkpoint_path: Path, caller: str, combined: bool
) -> None:
    if combined:
        gin.constant("loss_sep", True)
    else:
        gin.constant("loss_sep", False)
    CONFIG_PATH = CHECKPOINT_PATH.joinpath(checkpoint_path)
    with open(CONFIG_PATH / "config_info.txt", "r") as f:
        lines = f.readlines()
    if caller == "train":
        str_to_add = "import src.data_loader\n"
    else:
        str_to_add = "import bin.train\nimport src.model\nimport src.data_loader\n"
    with open(CONFIG_PATH / "config_temp.txt", "w") as f:
        f.writelines(str_to_add)
        f.writelines(lines[1:])
    config_name = str(CONFIG_PATH / "config_temp.txt")
    gin.parse_config_file(config_name)
    os.remove(config_name)
    if caller == "predict":
        pass
    else:
        print(f"Loaded gin config file from {CONFIG_PATH}/config_temp.txt")


def load_model_from_checkpoint(model, checkpoint_path: Path):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    ckpt_dict = torch.load(checkpoint_path, map_location=device)
    state_dict = ckpt_dict["state_dict"]
    model = model(gin.REQUIRED)
    model.load_state_dict(state_dict, strict=False)
    return model
