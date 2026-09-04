; Source-controlled 128K TAP loader. Based on the sjasmplus SAVETAP loader in
; io_tape_ldrs.h (zlib license), then reduced to the AYMax player bank layout.
; Included after SAVESNA, so page 5 changes do not change the snapshot.

TapBasic       equ #5D00
TapLoader      equ #5F00
RomCls         equ #0D6B
RomLoadBytes   equ #0556
TapPackLen0    equ PACK_BYTES <? 16384
TapAssetLen0   equ AssetUsed0
TapAssetLen1   equ AssetUsed1
TapProgramLen  equ StartEnd - Start

        assert TapPackLen0 > 0
        assert TapPackLen0 == PackEnd0 - AymaxPageBase
        assert TapAssetLen0 > 0
        assert TapAssetLen0 <= 16384
        assert TapAssetLen1 <= 16384
        assert TapProgramLen > 0
        assert AymaxProgramEnd <= AymaxRamEnd

        slot 1
        page 5
        org TapBasic
TapBasicStart
        db 0, 10                           ; line 10, big-endian
        dw .checkEnd - .check
.check
        db #FA, #BE, #B0, '"', "2899", '"' ; IF PEEK VAL "2899"
        db #C9, #B0, '"', "159", '"', #CB ; <> VAL "159" THEN
        db #EC, #B0, '"', "100", '"', #0D ; GO TO VAL "100"
.checkEnd

        db 0, 20                           ; line 20, big-endian
        dw .loadEnd - .load
.load
        db #E7, #B0, '"', "0", '"'        ; BORDER VAL "0"
        db ':', #DA, #B0, '"', "0", '"'   ; PAPER VAL "0"
        db ':', #FD, #B0, '"', "24319", '"' ; CLEAR VAL "24319"
        db ':', #FB                        ; CLS
        db ':', #EF, '"', '"', #AF         ; LOAD "" CODE
        db ':', #F9, #C0, #B0             ; RANDOMIZE USR VAL
        db '"', "24320", '"', #0D
.loadEnd

        db 0, 100                          ; line 100, big-endian
        dw .styleEnd - .style
.style
        db #E7, #B0, '"', "2", '"'         ; BORDER VAL "2"
        db ':', #DA, #B0, '"', "2", '"'    ; PAPER VAL "2"
        db ':', #D9, #B0, '"', "6", '"'    ; INK VAL "6"
        db ':', #DC, #B0, '"', "1", '"'    ; BRIGHT VAL "1"
        db ':', #FB, #0D                   ; CLS
.styleEnd

        db 0, 110                          ; line 110, big-endian
        dw .message1End - .message1
.message1
        db #F5, #27, '"'
        db " Please ensure you are running", '"', #27, '"'
        db " this on ZX Spectrum 128", '"', #27, '"'
        db " with AY-3-8912 chip."
        db '"', #0D
.message1End

        db 0, 120                          ; line 120, big-endian
        dw .message2End - .message2
.message2
        db #F5, #27, '"'
        db " If you are running this", '"', #27, '"'
        db " in an emulator, switch", '"', #27, '"'
        db " to ZX Spectrum 128", '"', #27, '"'
        db " in settings."
        db '"', #0D
.message2End

        db 0, 130                          ; line 130, big-endian
        dw .message3End - .message3
.message3
        db #F5, #27, '"'
        db " If you are running this on a", '"', #27, '"'
        db " real hardware ZX Spectrum 48,", '"', #27, '"'
        db " what did you expect?"
        db '"', #0D
.message3End

        db 0, 140                          ; line 140, big-endian
        dw .stopEnd - .stop
.stop
        db #E2, #0D                       ; STOP
.stopEnd
TapBasicEnd
        assert TapBasicEnd <= TapLoader

        org TapLoader
TapLoaderStart
        xor a
        ld (23693), a
        call RomCls

        ld a, AymaxPackBank
        call .page
        ld ix, AymaxPageBase
        ld de, PackEnd0 - AymaxPageBase
        call .load

    if PACK_BYTES > 16384
        ld a, AymaxPackBank + 1
        call .page
        ld ix, AymaxPageBase
        ld de, PackEnd1 - AymaxPageBase
        call .load
    endif

        ld a, AymaxAssetBank0
        call .page
        ld ix, AymaxPageBase
        ld de, TapAssetLen0
        call .load

    if TapAssetLen1 > 0
        ld a, AymaxAssetBank1
        call .page
        ld ix, AymaxPageBase
        ld de, TapAssetLen1
        call .load
    endif

        ; Run-once stub loads with no paging needed: bank 5 is always slot 1.
        ld ix, Start
        ld de, TapProgramLen
        call .load

        ld a, AymaxPackBank
        call .page
        ld hl, Start
        push hl
        ld ix, FastMem
        ld de, AymaxProgramEnd - FastMem
        jp .load

.page
        di
        or ROM_128K
        ld bc, #7FFD
        out (c), a
        ei
        ret

.load
        ld a, #FF
        scf
        jp RomLoadBytes
TapLoaderEnd
        assert TapLoaderEnd <= SlowMem

; The block order matches the calls above. HEADLESS writes raw ROM tape blocks.
        emptytap TAP_OUTPUT
        savetap TAP_OUTPUT, BASIC, "aymax", TapBasicStart, TapBasicEnd - TapBasicStart, 10
        savetap TAP_OUTPUT, CODE, "AYMax player", TapLoaderStart, TapLoaderEnd - TapLoaderStart, TapLoaderStart

        slot 3
        page AymaxPackBank
        org AymaxPageBase
        savetap TAP_OUTPUT, HEADLESS, AymaxPageBase, PackEnd0 - AymaxPageBase

    if PACK_BYTES > 16384
        page AymaxPackBank + 1
        org AymaxPageBase
        savetap TAP_OUTPUT, HEADLESS, AymaxPageBase, PackEnd1 - AymaxPageBase
    endif

        page AymaxAssetBank0
        org AymaxPageBase
        savetap TAP_OUTPUT, HEADLESS, AymaxPageBase, TapAssetLen0

    if TapAssetLen1 > 0
        page AymaxAssetBank1
        org AymaxPageBase
        savetap TAP_OUTPUT, HEADLESS, AymaxPageBase, TapAssetLen1
    endif

        slot 1
        page 5
        org Start
        savetap TAP_OUTPUT, HEADLESS, Start, TapProgramLen

        slot 2
        page 2
        org FastMem
        savetap TAP_OUTPUT, HEADLESS, FastMem, AymaxProgramEnd - FastMem
