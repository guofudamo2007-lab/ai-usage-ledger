"""Rebuild the small checked-in browser bundles from integrity-pinned upstream packages."""
import argparse
import base64
import hashlib
import io
import re
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "vendor"
PACKAGES = {
    "uplot": ("1.6.32", "KIMVnG68zvu5XXUbC4LQEPnhwOxBuLyW1AHtpm6IKTXImkbLgkMy+jabjLgSLMasNuGGzQm/ep3tOkyTxpiQIw=="),
    "tom-select": ("2.7.0", "i/iuTfrzYfGPAILfgPdU9/CCSm8XcnomjO2bHjAcFQPV7od0ghVngjaMfB5x5Agkrpe+49/tK6soegrwWyi6Lg=="),
    "@radix-ui/colors": ("3.0.0", "FUOsGBkHrYJwCSEtWRCIfQbZG7q1e6DgxCIOe1SUQzDe/7rXXeA47s8yCn6fuTNQAj1Zq4oTFi9Yjp3wzElcxg=="),
    "open-props": ("1.7.23", "+xyrJmxV9QUBFbSwPROYhOg/FLXry+uuPr4R+B5EaE4556Oc18/8ZC/fL5A+/lbRSwNvA0Alh+C0KpyFWLmLZg=="),
    "@phosphor-icons/core": ("2.1.1", "v4ARvrip4qBCImOE5rmPUylOEK4iiED9ZyKjcvzuezqMaiRASCHKcRIuvvxL/twvLpkfnEODCOJp5dM4eZilxQ=="),
    "@orchidjs/sifter": ("1.1.0", "mYwHCfr736cIWWdhhSZvDbf90AKt2xyrJspKFC3qyIJG1LtrJeJunYEqCGG4Aq2ijENbc4WkOjszcvNaIAS/pQ=="),
    "@orchidjs/unicode-variants": ("1.2.2", "Tfdm/fa2NN+75KwtkjpdOvUeaBOgZlyo08Ngb3m6UXe4ACPcKlgnX32vAI5lp39tlLdv2tXiRW0PPdbWyM5ieA=="),
}
ICONS = (
    "chart-line-up notebook plugs database arrows-clockwise sliders-horizontal clock calendar-blank "
    "caret-down caret-right arrow-left pulse stack info list-numbers globe-hemisphere-east "
    "download-simple upload-simple plus x image wallet squares-four pencil-simple check receipt "
    "link-simple arrow-down-left arrow-up-right memory sort-descending cube-transparent chart-donut "
    "terminal-window hard-drives cube arrow-square-out wave-sine stack-simple"
).split()


def bundle(cache):
    assets, css = {}, []
    for name, (version, digest) in PACKAGES.items():
        short = name.split("/")[-1]
        archive = cache / (short + ".tgz") if cache else None
        if archive and archive.is_file():
            raw = archive.read_bytes()
        else:
            url = f"https://registry.npmjs.org/{name}/-/{short}-{version}.tgz"
            with urllib.request.urlopen(url, timeout=45) as res:
                raw = res.read()
        if base64.b64encode(hashlib.sha512(raw).digest()).decode() != digest:
            raise ValueError(f"Integrity mismatch for {name} {version}")
        with tarfile.open(fileobj=io.BytesIO(raw)) as pkg:
            def content(path):
                return pkg.extractfile("package/" + path).read()
            if name == "@orchidjs/sifter":
                notice = content("README.md").split(b"## License", 1)[1].strip()
                assets["licenses/sifter.txt"] = notice + b"\n\n" + assets["licenses/tom-select.txt"]
            else:
                assets[f"licenses/{short}.txt"] = content("LICENSE")
            if name == "uplot":
                assets["uplot.js"] = content("dist/uPlot.iife.min.js")
                css.append(content("dist/uPlot.min.css"))
            elif name == "tom-select":
                assets["tom-select.js"] = content("dist/js/tom-select.base.min.js")
                css.append(content("dist/css/tom-select.css"))
            elif name == "@radix-ui/colors":
                css.extend(content(sheet) for sheet in ("slate-dark.css", "teal-dark.css"))
            elif name == "open-props":
                css.append(content("easings.min.css"))
            elif name == "@phosphor-icons/core":
                symbols = []
                for icon in ICONS:
                    src = content(f"assets/duotone/{icon}-duotone.svg").decode().strip()
                    shapes = src[src.index(">") + 1:src.rindex("</svg>")]
                    symbols.append(f'<symbol id="{icon}" viewBox="0 0 256 256">{shapes}</symbol>')
                assets["icons.svg"] = ('<svg xmlns="http://www.w3.org/2000/svg">' + "\n".join(symbols) + "</svg>").encode()
    assets["components.css"] = b"\n".join(css)
    for file, data in assets.items():
        path = OUT / file
        path.parent.mkdir(exist_ok=True, parents=True)
        if path.suffix in {".css", ".js"}:
            data = re.sub(rb"(?:/\*# sourceMappingURL=.*?\*/|//# sourceMappingURL=[^\r\n]+)", b"", data)
        path.write_bytes(data)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--cache", type=Path, help="Optional directory of downloaded package archives")
    bundle(cli.parse_args().cache)
