# ESP Anywhere Builder — staging

The add-on reads ESPHome projects from `/config/esphome` in Home Assistant and
keeps Builder metadata, build caches and local artifacts in its backed-up
`/data` volume. The Home Assistant configuration mount is read-only.

## Required options

- `installation_id`: installation identity registered by ESP Anywhere.
- `worker_ha_token`: Worker credential for that installation.
- `builder_token`: at least 32 random characters; also used by the Home
  Assistant ESP Anywhere integration when calling the Builder API.
- `firmware_signing_key`: PEM-encoded Ed25519 staging private key.
- `signing_key_id`: public key identifier trusted by the staging integration.

Do not use production device credentials or production signing keys with this
experimental add-on.

## First device

Open the add-on from the Home Assistant sidebar, add an existing YAML from the
ESPHome directory, select `VALIDATE`, then `INSTALL` and
`Plug into this computer`. Chrome or Edge is required for browser flashing and
serial provisioning. Later updates use `INSTALL` and `Wirelessly`.
