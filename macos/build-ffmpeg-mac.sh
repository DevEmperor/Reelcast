#!/bin/bash
# Minimal LGPL ffmpeg/ffprobe for Reelcast on macOS: MP3 (LAME), AAC/M4A, FLAC – static, no GPL parts.
set -euo pipefail
B=~/reelcast-dev/ffbuild; P=$B/prefix; mkdir -p $B/src $P; cd $B/src
LAME=lame-3.100; FF=ffmpeg-7.1.1
[ -f $LAME.tar.gz ] || curl -sSL -o $LAME.tar.gz "https://downloads.sourceforge.net/project/lame/lame/3.100/$LAME.tar.gz"
[ -f $FF.tar.xz ] || curl -sSL -o $FF.tar.xz "https://ffmpeg.org/releases/$FF.tar.xz"
shasum -a 256 $LAME.tar.gz $FF.tar.xz | tee $B/sha256.txt
rm -rf $FF && tar -xJf $FF.tar.xz && { [ -d $LAME ] || tar -xzf $LAME.tar.gz; }
export MACOSX_DEPLOYMENT_TARGET=14.2
[ -f $P/lib/libmp3lame.a ] || ( cd $LAME && ./configure --prefix=$P --disable-shared --enable-static --disable-frontend --disable-decoder >/dev/null && make -j8 >/dev/null && make install >/dev/null )
echo "LAME ok"
cd $FF
./configure --prefix=$P --disable-everything --disable-doc --disable-network --disable-autodetect \
  --disable-ffplay --enable-ffmpeg --enable-ffprobe --disable-debug --disable-x86asm \
  --enable-libmp3lame --extra-cflags="-I$P/include" --extra-ldflags="-L$P/lib" \
  --enable-encoder=libmp3lame,aac,flac,pcm_f32le,pcm_s16le \
  --enable-decoder=flac,mp3,mp3float,aac,pcm_f32le,pcm_s16le,pcm_s32le,mjpeg,png \
  --enable-muxer=mp3,flac,ipod,mp4,wav,null \
  --enable-demuxer=flac,mp3,mov,wav,pcm_f32le,image2,image_jpeg_pipe,image_png_pipe \
  --enable-parser=flac,mpegaudio,aac,mjpeg,png \
  --enable-filter=atrim,asetpts,concat,aresample,aformat,anull,null \
  --enable-protocol=file,pipe >/dev/null
make -j8 >/dev/null
cp -f ffmpeg ffprobe $B/
echo "FFMPEG ok"
$B/ffmpeg -hide_banner -encoders | grep -E "libmp3lame|aac|flac"
otool -L $B/ffmpeg | tail -n +2
