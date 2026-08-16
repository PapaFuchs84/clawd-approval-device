# Third-Party Notices

This project derives some assets and reference values from other MIT-licensed
open-source projects. Their license text is reproduced below as required by
the MIT license.

## Sprite artwork: marciogranzotto/clawd-tank

`firmware/tools/svg/*.svg` and the generated `firmware/include/sprites.h`
are derived from:

> https://github.com/marciogranzotto/clawd-tank

```
MIT License

Copyright (c) 2026 Marcio Granzotto Rodrigues

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Board pin / display reference values: BitMaker-hub/NerdMiner_v2

`firmware/platformio.ini` and the TFT_eSPI configuration in
`firmware/src/main.cpp` reuse pin and display-timing values (originally
from `src/drivers/devices/lilygoV1TDisplay.h` and
`lib/TFT_eSPI/User_Setups/Setup25_TTGO_T_Display.h`) from:

> https://github.com/BitMaker-hub/NerdMiner_v2

```
MIT License

Copyright (c) 2023 Bitmaker

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Libraries (via PlatformIO, not vendored in this repo)

- [TFT_eSPI](https://github.com/Bodmer/TFT_eSPI) (Bodmer) — FreeBSD-style license
- [arduinoWebSockets](https://github.com/Links2004/arduinoWebSockets) (Links2004) — Apache 2.0
- [ArduinoJson](https://github.com/bblanchon/ArduinoJson) (bblanchon) — MIT
- [OneButton](https://github.com/mathertel/OneButton) (mathertel) — BSD-2-Clause
- [websockets](https://github.com/python-websockets/websockets) (Python) — BSD-3-Clause
