# Getting started

For a reproducible installation, use `environment-lock.yml` rather than the unpinned `environment.yml`.

## Dashboard

From `dashboard/`:

```bash
conda-lock install --name synapse-gui environment-lock.yml
conda activate synapse-gui
export SF_DB_HOST='127.0.0.1'
export SF_DB_READONLY_PASSWORD='...'
export AM_SC_API_KEY='...'
python -u app.py --port 8080
```

For local MongoDB access, open a tunnel first:

```bash
ssh -L 27017:mongodb05.nersc.gov:27017 <username>@dtn03.nersc.gov -N
```

## ML training

Training requires an experiment configuration. Experiment configs are not part of this
repository: clone the private repository for your experiment into `experiments/` first, so
that `experiments/synapse-<experiment>/config.yaml` exists. See
[Experiment configuration](experiment-configuration.md) for the expected layout.

From `ml/`:

```bash
conda-lock install --name synapse-ml environment-lock.yml
conda activate synapse-ml
export SF_DB_READONLY_PASSWORD='...'
export AM_SC_API_KEY='...'
python train_model.py --test --config_file ../experiments/synapse-<experiment>/config.yaml --model NN
```

## Required environment variables

- `SF_DB_HOST`: MongoDB host for the dashboard.
- `SF_DB_READONLY_PASSWORD`: read-only MongoDB password.
- `AM_SC_API_KEY`: American Science Cloud MLflow API key when the config uses that service.
