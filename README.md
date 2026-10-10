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
  --bitcode-build "make -C build-bc -j$(nproc) && get-bc -b -o build-bc/lib.bc build-bc/lib.a && clang -c -emit-llvm -g -O0 -Xclang -disable-O0-optnone -I$KLEE_INCLUDE __locus_harness_*.c -o build-bc/harness.bc && llvm-link build-bc/lib.bc build-bc/harness.bc -o __locus_klee.bc" \
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

### KLEE validation

KLEE validation needs `klee` and `ktest-tool` on `PATH`, [gllvm](https://github.com/SRI-CSL/gllvm) (`gclang`, `get-bc`), and `clang` and `llvm-link` from the LLVM version your KLEE was built against. Set `KLEE_INCLUDE` to the directory that contains `klee/klee.h`.

For each predicate, Locus writes a harness to `__locus_harness_<dimension>.c` in the repo root, runs the `--bitcode-build` command, and runs KLEE with entry point `__locus_klee_harness_<dimension>` on the newest `.bc` file in the repo root. The command must therefore compile the harness and link it with the target's bitcode into one `.bc` file in the repo root. Bitcode of the target alone has no harness entry point. Only one harness exists at a time, so `__locus_harness_*.c` matches it. When the harness does not build or KLEE fails to run, Locus keeps the predicate, marks it as not validated, and logs the output.

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
cmake -B build -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
make -C build -j$(nproc)
ln -s build/compile_commands.json .

# 3. Build LLVM bitcode for KLEE
CC=gclang cmake -B build-bc -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
  -DCMAKE_C_FLAGS="-g -O0 -Xclang -disable-O0-optnone" \
  -DBUILD_PROGRAMS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF \
  -DENABLE_EXTERNAL_LIBS=OFF -DENABLE_MPEG=OFF
make -C build-bc -j$(nproc)
echo /__locus_klee.bc >> .git/info/exclude
git add -A && git commit -m "setup"

# 4. Run Locus
export KLEE_INCLUDE=/path/to/klee/include
uv run locus \
  --repo /tmp/snd/repo \
  --binary-build "make -C build -j$(nproc)" \
  --bitcode-build "make -C build-bc -j$(nproc) sndfile && get-bc -b -o build-bc/libsndfile.bc build-bc/libsndfile.a && clang -c -emit-llvm -g -O0 -Xclang -disable-O0-optnone -I$KLEE_INCLUDE -Iinclude -Ibuild-bc/include -Isrc -Ibuild-bc/src -include build-bc/src/config.h -include src/common.h '-DMAGMA_OR(a,b)=((a)||(b))' '-DMAGMA_AND(a,b)=((a)&&(b))' __locus_harness_*.c -o build-bc/harness.bc && llvm-link build-bc/libsndfile.bc build-bc/harness.bc -o __locus_klee.bc" \
  --min-iterations 3
```

Omit step 3 and `--bitcode-build` to run without KLEE validation. `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` is needed only with CMake 4 or later. The `-include` flags give the harness libsndfile constants such as `SF_MAX_CHANNELS`. The `-D` flags turn Magma's `MAGMA_OR` and `MAGMA_AND`, which the harness copies from the canary condition, into plain C operators; Magma defines them as calls to its runtime, which is not in the bitcode. The `.git/info/exclude` entry keeps the linked bitcode from dirtying the working tree for later runs.

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
