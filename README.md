# Locus

## Installation

Requires Python >= 3.13.

```bash
uv sync
uv run locus --help
```

## Usage

### CLI

```bash
uv run locus \
  --repo /path/to/target \
  --binary-build "make -C build -j$(nproc)" \
  --bitcode-build "make -C build-bc -j$(nproc) && get-bc -b build-bc/lib.a"
  --min-iterations 3 \
  --max-iterations 10
```

### CLI options

| Option | Default | Description |
|--------|---------|-------------|
| `--repo` | (required) | Path to target C/C++ codebase |
| `--binary-build` | (required) | Build command for compilation |
| `--bitcode-build` | — | Build command for LLVM bitcode (enables KLEE) |
| `--model` | `openai:o4-mini` | pydantic-ai model string |
| `--max-iterations` | 10 | Maximum refinement iterations |
| `--min-iterations` | 1 | Minimum iterations before agent can stop |
| `--verbose` | false | Enable debug logging to stderr |

### Predicate conventions

All inserted code follows these rules:
- Identifiers use `__locus_` prefix
- Comments use `// Locus:` prefix
- Guards use `_exit(1)` (not `exit()`)
- `#include <unistd.h>` added where needed

## Example: libsndfile SND001

Using the [Magma](https://hexhive.epfl.ch/magma/) benchmark:

```bash
# 1. Set up target with canary
git clone https://github.com/libsndfile/libsndfile.git /tmp/snd/repo
cd /tmp/snd/repo && git checkout 86c9f9eb
git apply magma/targets/libsndfile/patches/bugs/SND001.patch

# 2. Build with compile_commands.json
cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
make -C build -j$(nproc)
ln -s build/compile_commands.json .
git add -A && git commit -m "setup"

# 3. Run Locus
uv run locus \
  --repo /tmp/snd/repo \
  --binary-build "make -C build -j$(nproc)" \
  --min-iterations 3
```

Locus discovers the canary condition
`MAGMA_OR(paf_fmt.channels < 1, paf_fmt.channels > SF_MAX_CHANNELS)` and
inserts predicates for channels validation, format type, sample rate, version,
endianness, blockwidth safety, and more — placed as close to program entry as
data dependencies allow.

## Citation

```tex
@inproceedings{zhu2025locus,
    author={Zhu, Jie and Shen, Chihao and Li, Ziyang and Yu, Jiahao and Chen, Yizheng and Pei, Kexin},
    title={Locus: Agentic predicate synthesis for directed fuzzing},
    year = {2026},
    publisher = {Association for Computing Machinery},
    address = {New York, NY, USA},
    url = {https://doi.org/10.1145/3744916.3773102},
    doi = {10.1145/3744916.3773102},
    booktitle = {Proceedings of the ACM/IEEE 48nd International Conference on Software Engineering},
    location = {Rio de Janeiro, Brazil},
    series = {ICSE '26}
}
```
