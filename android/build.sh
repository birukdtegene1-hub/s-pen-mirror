#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
sdk=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}
platform="$sdk/platforms/android-35/android.jar"
bt="$sdk/build-tools/35.0.0"
if [[ ! -f "$platform" || ! -x "$bt/aapt2" ]]; then
    echo 'Set ANDROID_HOME to an Android SDK with platforms;android-35 and build-tools;35.0.0.' >&2
    exit 1
fi
out="$PWD/build"
mkdir -p "$out" ../dist
work=$(mktemp -d "$out/compile.XXXXXXXX")
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/gen" "$work/classes" "$work/dex" "$work/assets/licenses"
cp licenses/ZXing.txt "$work/assets/licenses/"
zxing=${ZXING_JAR:-"$out/zxing-core-3.5.3.jar"}
if [[ ! -f "$zxing" ]]; then
    curl -fsSL --retry 2 https://repo.maven.apache.org/maven2/com/google/zxing/core/3.5.3/core-3.5.3.jar -o "$work/zxing.jar"
    mv "$work/zxing.jar" "$zxing"
fi
echo "8d8064c1636fdaef7189dd9055c7d59950a8940a12f2293956446ec3c109fd82  $zxing" | sha256sum --check --status
"$bt/aapt2" compile --dir res -o "$work/res.zip"
"$bt/aapt2" link -I "$platform" --manifest AndroidManifest.xml --java "$work/gen" \
    -A "$work/assets" -o "$work/resources.apk" "$work/res.zip"
mapfile -t sources < <(find src "$work/gen" -type f -name '*.java' | sort)
javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options -bootclasspath "$platform:$bt/core-lambda-stubs.jar" \
    -classpath "$zxing" -d "$work/classes" "${sources[@]}"
jar cf "$work/app.jar" -C "$work/classes" .
"$bt/d8" --release --min-api 29 --lib "$platform" --output "$work/dex" "$work/app.jar" "$zxing"
(cd "$work/dex" && zip -q -0 "$work/resources.apk" classes.dex)
"$bt/zipalign" -f 4 "$work/resources.apk" "$work/aligned.apk"

# The local signing identity stays outside the distributable APK and source tree tracked by Git.
keys="$PWD/local-signing"
mkdir -p -m 700 "$keys"
exec 9>"$keys/build.lock"
flock 9
if [[ ! -f "$keys/relay.p12" ]]; then
    (umask 077
     openssl rand -hex 24 > "$keys/password"
     keytool -genkeypair -alias relay -keyalg RSA -keysize 3072 -validity 10000 \
         -storetype PKCS12 -keystore "$work/relay.p12" -storepass:file "$keys/password" \
         -dname "CN=S-Pen Mirror local build"
     mv "$work/relay.p12" "$keys/relay.p12")
fi
"$bt/apksigner" sign --ks "$keys/relay.p12" --ks-pass "file:$keys/password" --ks-key-alias relay \
    --v4-signing-enabled false --out "$work/signed.apk" "$work/aligned.apk"
"$bt/apksigner" verify --verbose "$work/signed.apk"
mv "$work/signed.apk" ../dist/S-Pen-Mirror.apk
echo "Built $(realpath ../dist/S-Pen-Mirror.apk)"
