; 128K TAP loader for the AYMax player. The loader code sits inside a BASIC
; REM statement: the ROM stores the program at PROG (23755), so the byte after
; the REM token of line 1 is at 23760 and RANDOMIZE USR 23760 runs it.
; Derived from the sjasmplus SAVETAP loader in io_tape_ldrs.h (zlib license).
; Included after SAVESNA, so page 5 changes do not change the snapshot.
;
; With ZX0 every bank block and the engine block are ZX0 streams. Each loads
; to ZxStage (#6100, above RAMTOP) and is unpacked to its place before the
; next block loads. The screen loads as it is.

Prog           equ 23755                  ; PROG on load
RomCls         equ #0D6B
RomLoadBytes   equ #0556
TapAssetLen0   equ AssetUsed0
TapAssetLen1   equ AssetUsed1
TapProgramLen  equ AymaxProgramEnd - FastMem
ZxStage        equ #6100                ; #6100-#BFFF, above RAMTOP: compressed block staging

        assert TapAssetLen0 > 0 || !AssetNeeded
        assert TapAssetLen0 <= 16384
        assert TapAssetLen1 <= 16384
        assert AymaxProgramEnd <= AymaxRamEnd

; Load one block to dest (len bytes). ZX0: load zxlen bytes to ZxStage, unpack to dest.
        macro LOADBLK dest, len, zxlen
    ifdef ZX0
        ld de, zxlen
        ld hl, dest
        call TapUnpack
    else
        ld ix, dest
        ld de, len
        call TapLoad
    endif
        endm

        slot 1
        page 5
        org Prog
TapBasicStart
        db 0, 1                            ; line 1, big-endian
        dw TapRemEnd - TapRem
TapRem
        db #EA                             ; REM
TapLoaderStart
        assert TapLoaderStart == 23760
        xor a
        ld (23693), a
        call RomCls

    ifdef SCREEN_FILE
        ld ix, #4000
        ld de, #1B00
        call TapLoad
    endif

    if !PackInAssets
        ld a, AymaxPackBank
        call TapPage
        LOADBLK AymaxPageBase, PackEnd0 - AymaxPageBase, TapPack0Zx

    if PackBytes > 16384
        ld a, AymaxPackBank + 1
        call TapPage
        LOADBLK AymaxPageBase, PackEnd1 - AymaxPageBase, TapPack1Zx
    endif
    endif

    if AssetNeeded
        ld a, AymaxAssetBank0
        call TapPage
        LOADBLK AymaxPageBase, TapAssetLen0, TapAsset0Zx

    if TapAssetLen1 > 0
        ld a, AymaxAssetBank1
        call TapPage
        LOADBLK AymaxPageBase, TapAssetLen1, TapAsset1Zx
    endif
    endif

        ; Engine loads with no paging needed: bank 2 is always slot 2.
        LOADBLK FastMem, TapProgramLen, TapProgramZx
        PLAYER_STUB

TapPage
        di
        or ROM_128K
        ld bc, #7FFD
        out (c), a
        ei
        ret

TapLoad
        ld a, #FF
        scf
        jp RomLoadBytes

    ifdef ZX0
; In: de = compressed length, hl = destination.
TapUnpack
        push hl
        ld ix, ZxStage
        call TapLoad
        ld hl, ZxStage
        pop de
        jp dzx0_standard
        include "dzx0_standard.asm"
    endif
        db #0D
TapRemEnd

    ifndef COMPACT_LOADER
        db 0, 10                           ; line 10, big-endian
        dw .checkEnd - .check
.check
        db #FA, #BE, #B0, '"', "2899", '"' ; IF PEEK VAL "2899"
        db #C9, #B0, '"', "159", '"', #CB ; <> VAL "159" THEN
        db #EC, #B0, '"', "100", '"', #0D ; GO TO VAL "100"
.checkEnd
    endif

        db 0, 20                           ; line 20, big-endian
        dw .runEnd - .run
.run
        db #E7, #B0, '"', "0", '"'        ; BORDER VAL "0"
        db ':', #DA, #B0, '"', "0", '"'   ; PAPER VAL "0"
        db ':', #FD, #B0, '"', "24831", '"' ; CLEAR VAL "24831" (RAMTOP #60FF, stack below ZxStage)
        db ':', #FB                        ; CLS
        db ':', #F9, #C0, #B0             ; RANDOMIZE USR VAL
        db '"', "23760", '"', #0D
.runEnd
    ifndef COMPACT_LOADER
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
    endif ; COMPACT_LOADER
TapBasicEnd
        assert TapBasicEnd <= 24831 - 256   ; leave room for the BASIC stack under RAMTOP

; The block order matches the calls above. HEADLESS writes raw ROM tape blocks.
        emptytap TAP_OUTPUT
    ifdef COMPACT_LOADER
        savetap TAP_OUTPUT, BASIC, "AYMAX play", TapBasicStart, TapBasicEnd - TapBasicStart, 20
    else
        savetap TAP_OUTPUT, BASIC, "AYMAX play", TapBasicStart, TapBasicEnd - TapBasicStart, 10
    endif

    ifdef SCREEN_FILE
        slot 1
        page 5
        org #4000
        savetap TAP_OUTPUT, HEADLESS, #4000, #1B00
    endif

    ifdef ZX0
; The compressed blocks stage at ZxStage (pages 5 and 2, saved already) for
; savetap, the same place the loader puts them.
        slot 1
        page 5
    if !PackInAssets
        org ZxStage
        incbin PACK0_ZX0
TapPack0Zx equ $ - ZxStage
        assert ZxStage + TapPack0Zx <= AymaxPageBase
        savetap TAP_OUTPUT, HEADLESS, ZxStage, TapPack0Zx

    if PackBytes > 16384
        org ZxStage
        incbin PACK1_ZX0
TapPack1Zx equ $ - ZxStage
        assert ZxStage + TapPack1Zx <= AymaxPageBase
        savetap TAP_OUTPUT, HEADLESS, ZxStage, TapPack1Zx
    endif
    endif

    if AssetNeeded
        org ZxStage
        incbin ASSET0_ZX0
TapAsset0Zx equ $ - ZxStage
        assert ZxStage + TapAsset0Zx <= AymaxPageBase
        savetap TAP_OUTPUT, HEADLESS, ZxStage, TapAsset0Zx

    if TapAssetLen1 > 0
        org ZxStage
        incbin ASSET1_ZX0
TapAsset1Zx equ $ - ZxStage
        assert ZxStage + TapAsset1Zx <= AymaxPageBase
        savetap TAP_OUTPUT, HEADLESS, ZxStage, TapAsset1Zx
    endif
    endif

        org ZxStage
        incbin ENGINE_ZX0
TapProgramZx equ $ - ZxStage
        assert ZxStage + TapProgramZx <= FastMem
        savetap TAP_OUTPUT, HEADLESS, ZxStage, TapProgramZx
    else
        slot 3
    if !PackInAssets
        page AymaxPackBank
        org AymaxPageBase
        savetap TAP_OUTPUT, HEADLESS, AymaxPageBase, PackEnd0 - AymaxPageBase

    if PackBytes > 16384
        page AymaxPackBank + 1
        org AymaxPageBase
        savetap TAP_OUTPUT, HEADLESS, AymaxPageBase, PackEnd1 - AymaxPageBase
    endif
    endif

    if AssetNeeded
        page AymaxAssetBank0
        org AymaxPageBase
        savetap TAP_OUTPUT, HEADLESS, AymaxPageBase, TapAssetLen0

    if TapAssetLen1 > 0
        page AymaxAssetBank1
        org AymaxPageBase
        savetap TAP_OUTPUT, HEADLESS, AymaxPageBase, TapAssetLen1
    endif
    endif

        slot 2
        page 2
        org FastMem
        savetap TAP_OUTPUT, HEADLESS, FastMem, TapProgramLen
    endif
