#!/bin/sh
# Rustwright CLI installer.
#
#   curl -fsSL https://raw.githubusercontent.com/Skyvern-AI/rustwright/main/install.sh | sh
#
# Downloads a prebuilt `rustwright-cli` binary for the current platform. When no
# prebuilt binary is published for the platform (or the download fails), it
# falls back to building from source with cargo.
set -eu

REPO="Skyvern-AI/rustwright"
BIN="rustwright-cli"
INSTALL_DIR="${RUSTWRIGHT_INSTALL_DIR:-$HOME/.local/bin}"

say() { printf '%s\n' "$*" >&2; }
err() {
    say "install error: $*"
    exit 1
}

# Map the host OS/arch onto the Rust target triple used for release asset names.
detect_target() {
    os="$(uname -s)"
    arch="$(uname -m)"
    case "$os" in
        Linux) os_part="unknown-linux-gnu" ;;
        Darwin) os_part="apple-darwin" ;;
        *) return 1 ;;
    esac
    case "$arch" in
        x86_64 | amd64) arch_part="x86_64" ;;
        arm64 | aarch64) arch_part="aarch64" ;;
        *) return 1 ;;
    esac
    printf '%s-%s' "$arch_part" "$os_part"
}

install_prebuilt() {
    target="$1"
    asset="${BIN}-${target}"
    url="https://github.com/${REPO}/releases/latest/download/${asset}"
    tmp="$(mktemp)"
    say "downloading ${asset} ..."
    if ! curl -fsSL "$url" -o "$tmp" 2>/dev/null; then
        rm -f "$tmp"
        return 1
    fi
    mkdir -p "$INSTALL_DIR"
    chmod +x "$tmp"
    mv "$tmp" "$INSTALL_DIR/$BIN"
    say "installed $BIN to $INSTALL_DIR/$BIN"
    return 0
}

install_from_source() {
    command -v cargo >/dev/null 2>&1 ||
        err "no prebuilt binary for this platform and cargo is not installed. Install Rust from https://rustup.rs and re-run."
    say "building $BIN from source with cargo (this may take a few minutes) ..."
    # Build into a temporary --root so the binary honors INSTALL_DIR rather than
    # landing in cargo's default bin directory.
    tmproot="$(mktemp -d)"
    if ! cargo install --git "https://github.com/${REPO}" "$BIN" --root "$tmproot"; then
        rm -rf "$tmproot"
        err "cargo install failed"
    fi
    mkdir -p "$INSTALL_DIR"
    mv "$tmproot/bin/$BIN" "$INSTALL_DIR/$BIN"
    rm -rf "$tmproot"
    say "installed $BIN to $INSTALL_DIR/$BIN"
}

main() {
    if target="$(detect_target)" && install_prebuilt "$target"; then
        :
    else
        say "prebuilt binary unavailable; falling back to building from source."
        install_from_source
    fi

    case ":${PATH}:" in
        *":$INSTALL_DIR:"*) : ;;
        *)
            say ""
            say "note: $INSTALL_DIR is not on your PATH. Add it with:"
            say "  export PATH=\"$INSTALL_DIR:\$PATH\""
            ;;
    esac
    say ""
    say "done. Try: $BIN open https://example.com"
}

main "$@"
