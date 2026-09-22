TAYM       ?= examples/atarized.taym
PYTHON     ?= python3
SJASMPLUS  ?= sjasmplus
PLAY_ONCE  ?= 0
BORDER     ?= 1
SCREEN     ?=
COMPACT    ?= 0
ENGINE     ?= auto
ZX0        ?= 0
ZX0C       ?= build/zx0

NAME := $(basename $(notdir $(TAYM)))
BUILD := build/$(NAME)

.PHONY: all clean setup force
all: $(BUILD).tap $(BUILD).sna

# taym2aymax: TAYM -> unpacked psg/events/samples/wavetables
$(BUILD)_psg.bin $(BUILD)_events.bin $(BUILD)_samples.bin $(BUILD)_wavetables.bin: $(TAYM)
	$(PYTHON) scripts/taym2aymax.py $(TAYM) -o $(BUILD)

# psgpack: unpacked data -> pack + assets + dir.inc
$(BUILD).pack $(BUILD)_assets.bin $(BUILD)_dir.inc: $(BUILD)_psg.bin $(BUILD)_events.bin \
		$(BUILD)_samples.bin $(BUILD)_wavetables.bin
	$(PYTHON) scripts/psgpack.py $(BUILD) --dir-asm $(BUILD)_dir.asm --pack-in-assets

# ZX0 compressor, built from the submodule (its own Makefile targets OpenWatcom).
$(ZX0C): zx0/src/zx0.c zx0/src/optimize.c zx0/src/compress.c zx0/src/memory.c zx0/src/zx0.h
	$(CC) -O2 -o $@ $(filter %.c,$^)

# player.asm + loader.asm build both .sna and .tap in one sjasmplus run.
# Always re-assembled (fast) so PLAY_ONCE, BORDER and SCREEN changes take effect.
# ENGINE=auto picks the smallest engine binary whose kernel set (AymaxKernels
# in its .inc) covers the kernels the track uses (KernelMask in _dir.inc).
# ZX0=1 compresses the engine, the used part of each asset bank, and the pack
# banks (16 KiB slices); the tape loader unpacks them (see loader.asm). The
# .sna is never compressed.
$(BUILD).sna: $(BUILD).tap
$(BUILD).tap: src/player.asm src/loader.asm $(wildcard bin/aymax_player*.bin bin/aymax_player*.inc) \
		$(BUILD).pack $(BUILD)_assets.bin $(BUILD)_dir.inc $(SCREEN) \
		$(if $(filter 1,$(ZX0)),$(ZX0C)) force
	@if [ "$(ENGINE)" = auto ]; then \
		need=$$(sed -n 's/^KernelMask equ //p' $(BUILD)_dir.inc); best=; size=0; \
		for inc in bin/aymax_player*.inc; do \
			have=$$(sed -n 's/^AymaxKernels: EQU //p' $$inc); \
			[ $$(( need & ~have )) -eq 0 ] || continue; \
			b=$${inc%.inc}.bin; n=$$(wc -c < $$b); \
			if [ -z "$$best" ] || [ $$n -lt $$size ]; then best=$${b%.bin}; size=$$n; fi; \
		done; \
		bin=$${best#bin/}; \
	elif [ "$(ENGINE)" = full ]; then bin=aymax_player; else bin=aymax_player_$(ENGINE); fi; \
	echo "engine: $$bin.bin"; \
	if [ "$(ZX0)" = 1 ]; then \
		u0=$$(sed -n 's/^AssetUsed0 equ //p' $(BUILD)_dir.inc); \
		u1=$$(sed -n 's/^AssetUsed1 equ //p' $(BUILD)_dir.inc); \
		pb=$$(sed -n 's/^PackBank equ //p' $(BUILD)_dir.inc); \
		pn=$$(sed -n 's/^PackBytes equ //p' $(BUILD)_dir.inc); \
		$(ZX0C) -f bin/$$bin.bin $(BUILD)_engine.zx0; \
		if [ $$u0 -gt 0 ]; then head -c $$u0 $(BUILD)_assets.bin > $(BUILD)_assets0.bin; \
			$(ZX0C) -f $(BUILD)_assets0.bin $(BUILD)_assets0.zx0; fi; \
		if [ $$u1 -gt 0 ]; then tail -c +16385 $(BUILD)_assets.bin | head -c $$u1 > $(BUILD)_assets1.bin; \
			$(ZX0C) -f $(BUILD)_assets1.bin $(BUILD)_assets1.zx0; fi; \
		if [ $$pb -eq 0 ]; then head -c 16384 $(BUILD).pack > $(BUILD)_pack0.bin; \
			$(ZX0C) -f $(BUILD)_pack0.bin $(BUILD)_pack0.zx0; fi; \
		if [ $$pb -eq 0 ] && [ $$pn -gt 16384 ]; then tail -c +16385 $(BUILD).pack > $(BUILD)_pack1.bin; \
			$(ZX0C) -f $(BUILD)_pack1.bin $(BUILD)_pack1.zx0; fi; \
	fi; \
	$(SJASMPLUS) --inc=src --inc=bin --inc=zx0/z80 --msg=war \
		-DENGINE_BIN="\"$$bin.bin\"" -DENGINE_INC="\"$$bin.inc\"" \
		-DPACK_FILE='"$(BUILD).pack"' \
		-DASSET_FILE='"$(BUILD)_assets.bin"' \
		-DDIR_INC='"$(BUILD)_dir.inc"' \
		-DSNA_OUTPUT='"$(BUILD).sna"' \
		-DTAP_OUTPUT='"$(BUILD).tap"' \
		$(if $(filter 1,$(PLAY_ONCE)),-DPLAY_ONCE) \
		$(if $(filter 0,$(BORDER)),-DNO_BORDER) \
		$(if $(SCREEN),-DSCREEN_FILE='"$(SCREEN)"') \
		$(if $(filter 1,$(COMPACT)),-DCOMPACT_LOADER) \
		$(if $(filter 1,$(ZX0)),-DZX0 -DENGINE_ZX0='"$(BUILD)_engine.zx0"' \
			-DASSET0_ZX0='"$(BUILD)_assets0.zx0"' -DASSET1_ZX0='"$(BUILD)_assets1.zx0"' \
			-DPACK0_ZX0='"$(BUILD)_pack0.zx0"' -DPACK1_ZX0='"$(BUILD)_pack1.zx0"') \
		src/player.asm

force:

clean:
	rm -rf build

setup:
	git submodule update --init --recursive
