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
;   ENGINE_BIN, ENGINE_INC (optional) -- engine variant, default the full one
;   PLAY_ONCE (optional) -- play the track once instead of looping
;   NO_BORDER (optional) -- keep the border black instead of the noise effect
;   SCREEN_FILE (optional) -- 6912-byte .scr shown while the track plays
;   COMPACT_LOADER (optional) -- BASIC loader without the 128K check
        device zxspectrum128

    ifndef ENGINE_BIN
        define ENGINE_BIN "aymax_player.bin"
    endif
    ifndef ENGINE_INC
        define ENGINE_INC "aymax_player.inc"
    endif

; ZX Spectrum 128K memory map (subset needed here).
SlowMem     equ #6000           ; contended RAM
FastMem     equ #8000           ; uncontended fast RAM

ROM_128K    equ %00010000       ; #7FFD bit

        include ENGINE_INC
        include DIR_INC

; The engine variant must have every kernel the track uses (ENGINE=auto
; picks one that does; a forced ENGINE= may not).
    if KernelMask & ~AymaxKernels
        display "error: the track uses kernels this engine variant lacks (track mask ", /D, KernelMask, ", engine ", /D, AymaxKernels, "); use ENGINE=auto or ENGINE=full"
        assert 0
    endif

; Engine binary: ORG #8000, self-contained.
        slot 2
        org FastMem
        incbin ENGINE_BIN
        assert $ == AymaxProgramEnd

; Packed track. psgpack appends it to the last asset bank when it fits
; (PackBank/PackOffset in DIR_INC); otherwise it is a separate file in
; bank AymaxPackBank at AymaxPageBase, overflow to bank + 1.
PackInAssets equ PackBank == AymaxAssetBank0 || PackBank == AymaxAssetBank1
        slot 3
    if !PackInAssets
        assert PackBank == AymaxPackBank && PackOffset == 0
        page AymaxPackBank
        org AymaxPageBase
        if PackBytes > 16384
            incbin PACK_FILE, 0, 16384
        else
            incbin PACK_FILE
        endif
PackEnd0 equ $
        if PackBytes > 16384
            page AymaxPackBank + 1
            org AymaxPageBase
            incbin PACK_FILE, 16384
PackEnd1 equ $
            assert PackEnd1 <= #10000
        endif
        assert PackBytes <= 32768
    endif

; Shared sample/wavetable assets (plus the pack when PackInAssets): bank
; AymaxAssetBank0, overflow to AymaxAssetBank1. A track without samples
; and wavetables needs no asset bank unless the pack lives there.
AssetNeeded equ PackInAssets || SampleCount || WavetableCount
    if AssetNeeded
        page AymaxAssetBank0
        org AymaxPageBase
        incbin ASSET_FILE, 0, 16384
    if AssetBankCount > 1
        page AymaxAssetBank1
        org AymaxPageBase
        incbin ASSET_FILE, 16384, 16384
    endif
    endif

; Optional screen, shown while the track plays.
        slot 1
        page 5
    ifdef SCREEN_FILE
        org #4000
        incbin SCREEN_FILE
        assert $ == #5B00
    endif

; Run-once stub right behind the engine in bank 2, so one tape block holds
; both. It sits in engine RAM: it runs before the engine touches that RAM
; and never returns. Points the engine at the pack, selects the loop mode.
        slot 2
        page 2
        org AymaxProgramEnd
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
        ld hl, AymaxPageBase + PackOffset
        ld (AymaxPackStart), hl
        ld a, ROM_128K | PackBank
        ld (AymaxPackBankInit), a
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
        assert StartEnd <= AymaxRamEnd

        savesna SNA_OUTPUT, Start

        include "loader.asm"
