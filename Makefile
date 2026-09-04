TAYM       ?= examples/atarized.taym
PYTHON     ?= python3
SJASMPLUS  ?= sjasmplus
PLAY_ONCE  ?= 0

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
	$(PYTHON) scripts/psgpack.py $(BUILD) --dir-asm $(BUILD)_dir.asm

# player.asm + loader.asm build both .sna and .tap in one sjasmplus run.
# Always re-assembled (fast) so PLAY_ONCE changes take effect.
$(BUILD).tap $(BUILD).sna: src/player.asm src/loader.asm bin/aymax_player.bin bin/aymax_player.inc \
		$(BUILD).pack $(BUILD)_assets.bin $(BUILD)_dir.inc force
	$(SJASMPLUS) --inc=src --inc=bin --msg=war \
		-DPACK_FILE='"$(BUILD).pack"' \
		-DASSET_FILE='"$(BUILD)_assets.bin"' \
		-DDIR_INC='"$(BUILD)_dir.inc"' \
		-DSNA_OUTPUT='"$(BUILD).sna"' \
		-DTAP_OUTPUT='"$(BUILD).tap"' \
		$(if $(filter 1,$(PLAY_ONCE)),-DPLAY_ONCE) \
		src/player.asm

force:

clean:
	rm -rf build

setup:
	git submodule update --init --recursive
