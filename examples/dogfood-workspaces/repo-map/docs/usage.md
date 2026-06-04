# Repo Map Usage

The capture trajectory should touch several parts of this repository so the
renderer can draw a city whose blocks correspond to top-level areas.

Run the local tests:

```bash
python -m pytest
```

Run the CLI against the sample fixture:

```bash
python -m repo_map.cli fixtures/tasks.txt
```

The interesting display data is not the application behavior. The useful signal
is the repository shape:

- source files under `src/repo_map`;
- tests under `tests`;
- documentation under `docs`;
- sample data under `fixtures`;
- command helpers under `scripts`.

The fixture intentionally keeps code modest while making documentation and
fixture files longer than the package marker. This lets line counts influence
building height without needing to store large files in the repository.
