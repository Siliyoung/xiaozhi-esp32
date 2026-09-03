# Third-party notices

## Noto Animated Emoji

This project packages resized GIF derivatives of Google Noto Animated Emoji for
the embedded display. The original animated assets are published by Google at
<https://googlefonts.github.io/noto-emoji-animation/>. Noto image resources are
available under the Apache License 2.0; Noto font resources are available under
the SIL Open Font License 1.1.

The bundled derivatives are resized and GIF-optimized for ESP32 playback. See
`managed_components/78__xiaozhi-fonts/generate_gifs.py` for the documented
conversion process and source URLs.

## Open-Meteo

Standby-clock and assistant weather data are provided by
[Open-Meteo](https://open-meteo.com/). Weather data are available under the
Creative Commons Attribution 4.0 International (CC BY 4.0) licence.

## IPWhois.io

City-level automatic location is resolved through the free
[IPWhois.io](https://ipwhois.io/) API. The device public IP is used only for
the lookup and is not included in assistant responses or application logs.
