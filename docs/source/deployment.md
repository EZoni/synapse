# Deployment

Synapse is deployed using Docker images and NERSC services.

## Dashboard image

From the repository root, build the image defined in {repo}`dashboard.Dockerfile`:

```bash
docker build --platform linux/amd64 --output type=image,oci-mediatypes=true -t synapse-gui -f dashboard.Dockerfile .
```

## ML image

From the repository root, build the image defined in {repo}`ml.Dockerfile`:

```bash
docker build --platform linux/amd64 --output type=image,oci-mediatypes=true -t synapse-ml -f ml.Dockerfile .
```

The two build commands differ only by image tag and Dockerfile.

## Publish helper

{repo}`publish_container.py` builds and pushes both images:

```bash
python publish_container.py --gui --ml
```

## NERSC assumptions

- Dashboard runs on Spin.
- Training and simulations run on Perlmutter through Superfacility API.
- Images are pushed to `registry.nersc.gov/m558/superfacility`.
- Before publishing, validate the images locally.
