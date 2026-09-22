TAYM       ?= examples/atarized.taym
PYTHON     ?= python3
SJASMPLUS  ?= sjasmplus
PLAY_ONCE  ?= 0
BORDER     ?= 1
SCREEN     ?=
COMPACT    ?= 0
ENGINE     ?= auto

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

# player.asm + loader.asm build both .sna and .tap in one sjasmplus run.
# Always re-assembled (fast) so PLAY_ONCE, BORDER and SCREEN changes take effect.
# ENGINE=auto picks the smallest engine binary whose kernel set (AymaxKernels
# in its .inc) covers the kernels the track uses (KernelMask in _dir.inc).
$(BUILD).sna: $(BUILD).tap
$(BUILD).tap: src/player.asm src/loader.asm $(wildcard bin/aymax_player*.bin bin/aymax_player*.inc) \
		$(BUILD).pack $(BUILD)_assets.bin $(BUILD)_dir.inc $(SCREEN) force
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
	$(SJASMPLUS) --inc=src --inc=bin --msg=war \
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
		src/player.asm

force:

clean:
	rm -rf build

setup:
	git submodule update --init --recursive
