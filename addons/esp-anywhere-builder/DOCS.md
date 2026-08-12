# ESP Anywhere Builder — staging

The add-on reads ESPHome projects from `/config/esphome` in Home Assistant and
keeps Builder metadata, build caches and local artifacts in its backed-up
`/data` volume. The Home Assistant configuration mount is read-only.

## First start

No add-on options are required. On first start the add-on finds the existing
ESP Anywhere integration in Home Assistant, connects it to the staging Worker,
and provisions its Builder credential and staging signing key. Credentials are
stored in the add-on's private `/data` volume and are not shown in the options
form or logs.

If ESP Anywhere is not configured yet, add the integration in Home Assistant
first and then start the add-on again. The staging add-on requires exactly one
ESP Anywhere integration configured for the staging service.

## First device

Open the add-on from the Home Assistant sidebar, add an existing YAML from the
ESPHome directory, select `VALIDATE`, then `INSTALL` and
`Plug into this computer`. Chrome or Edge is required for browser flashing and
serial provisioning. Later updates use `INSTALL` and `Wirelessly`.
