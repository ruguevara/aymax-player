;      ______    ____       ____________      ________     ______      ____      ____
;     /      \   \   \     /   /|       \    /       |    /      \     \   \    /   /
;    /   /\   \    \   \ /   /  |   |\   \  /   /|   |   /   /\   \      \   \/   /
;   /   /__\   \     \  '  /    |   | \   \/   / |   |  /   /__\   \      /      \
;  /    ____    \     |   |     |   |  \      /  |   | /    ____    \   /   /  \   \
; /____/    \____\    |___|     |___|   \____/   |___|/____/    \____\/____/    \____\
;
; AYMax public player wrapper. Loads the prebuilt engine plus a converted
; track, then jumps into the engine. Assemble with sjasmplus, defines:
;   PACK_FILE, ASSET_FILE, DIR_INC, SNA_OUTPUT, TAP_OUTPUT (quoted paths)
;   PLAY_ONCE (optional) -- play the track once instead of looping
;   NO_BORDER (optional) -- keep the border black instead of the noise effect
;   SCREEN_FILE (optional) -- 6912-byte .scr shown while the track plays
        device zxspectrum128

; ZX Spectrum 128K memory map (subset needed here).
SlowMem     equ #6000           ; contended RAM
FastMem     equ #8000           ; uncontended fast RAM

ROM_128K    equ %00010000       ; #7FFD bit

        include "aymax_player.inc"
        include DIR_INC

; Engine binary: ORG #8000, self-contained.
        slot 2
        org FastMem
        incbin "aymax_player.bin"
        assert $ == AymaxProgramEnd

; Packed track: bank AymaxPackBank at AymaxPageBase, overflow to bank + 1.
        lua allpass
            local path = sj.get_define("PACK_FILE")
            path = path:gsub('"', '')
            local f = assert(io.open(path, "rb"))
            local n = f:seek("end")
            f:close()
            sj.insert_label("PACK_BYTES", n)
        endlua

        slot 3
        page AymaxPackBank
        org AymaxPageBase
PackStart
        if PACK_BYTES > 16384
            incbin PACK_FILE, 0, 16384
        else
            incbin PACK_FILE
        endif
PackEnd0 equ $
        assert PackEnd0 <= #10000
        if PACK_BYTES > 16384
            page AymaxPackBank + 1
            org AymaxPageBase
            incbin PACK_FILE, 16384
PackEnd1 equ $
            assert PackEnd1 <= #10000
        else
PackEnd1 equ PackStart
        endif
        assert PACK_BYTES <= 32768

; Shared sample/wavetable assets: bank AymaxAssetBank0, overflow to bank 1.
        page AymaxAssetBank0
        org AymaxPageBase
        incbin ASSET_FILE, 0, 16384
    if AssetBankCount > 1
        page AymaxAssetBank1
        org AymaxPageBase
        incbin ASSET_FILE, 16384, 16384
    endif

; Optional screen, shown while the track plays.
        slot 1
        page 5
    ifdef SCREEN_FILE
        org #4000
        incbin SCREEN_FILE
        assert $ == #5B00
    endif

; Run-once stub in slow RAM (page 5): select the loop mode, enter the engine.
        org #6000
Start
        di
        xor a
        out (#FE), a                    ; border 0
    ifndef SCREEN_FILE
        ld hl, #4000                    ; black screen: pixels 0, attrs paper 0 ink 0
        ld de, #4001
        ld bc, #1AFF
        ld (hl), a
        ldir
    endif
    ifdef PLAY_ONCE
        ld hl, AymaxStopReset
        ld (AymaxLoopImm), hl
    endif
    ifdef NO_BORDER
        ld a, #DB                       ; out (254),a -> in a,(254): same size and timing
        ld (AymaxBorderOp), a
    endif
        jp AymaxPlayerStart
StartEnd

        savesna SNA_OUTPUT, Start

        include "loader.asm"
