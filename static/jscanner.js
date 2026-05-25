(function () {
    'use strict';

    function detectQuad(img) {
        const naturalW = img.naturalWidth;
        const naturalH = img.naturalHeight;
        const maxDim = 600;
        const scale = Math.min(1, maxDim / Math.max(naturalW, naturalH));
        const w = Math.max(1, Math.round(naturalW * scale));
        const h = Math.max(1, Math.round(naturalH * scale));
        const canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0, w, h);
        const imageData = ctx.getImageData(0, 0, w, h);

        const gray = new Uint8ClampedArray(w * h);
        const d = imageData.data;
        for (let i = 0; i < w * h; i++) {
            gray[i] = (d[i * 4] * 0.299 + d[i * 4 + 1] * 0.587 + d[i * 4 + 2] * 0.114) | 0;
        }
        const blur = new Uint8ClampedArray(w * h);
        for (let y = 1; y < h - 1; y++) {
            for (let x = 1; x < w - 1; x++) {
                let s = 0;
                for (let dy = -1; dy <= 1; dy++) {
                    for (let dx = -1; dx <= 1; dx++) {
                        s += gray[(y + dy) * w + (x + dx)];
                    }
                }
                blur[y * w + x] = (s / 9) | 0;
            }
        }
        const hist = new Array(256).fill(0);
        for (let i = 0; i < w * h; i++) hist[blur[i]]++;
        const total = w * h;
        let sum = 0;
        for (let i = 0; i < 256; i++) sum += i * hist[i];
        let sumB = 0, wB = 0, wF = 0, maxVar = 0, threshold = 128;
        for (let t = 0; t < 256; t++) {
            wB += hist[t];
            if (!wB) continue;
            wF = total - wB;
            if (!wF) break;
            sumB += t * hist[t];
            const mB = sumB / wB;
            const mF = (sum - sumB) / wF;
            const between = wB * wF * (mB - mF) * (mB - mF);
            if (between > maxVar) { maxVar = between; threshold = t; }
        }
        threshold = Math.max(threshold - 10, 64);
        const mask = new Uint8Array(w * h);
        for (let i = 0; i < w * h; i++) {
            mask[i] = blur[i] >= threshold ? 1 : 0;
        }

        const labels = new Int32Array(w * h);
        let nextLabel = 1;
        const parent = [0];
        const find = (x) => { while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x]; } return x; };
        const union = (a, b) => { a = find(a); b = find(b); if (a !== b) parent[b] = a; };
        for (let y = 0; y < h; y++) {
            for (let x = 0; x < w; x++) {
                const i = y * w + x;
                if (!mask[i]) continue;
                const left = x > 0 ? labels[i - 1] : 0;
                const up = y > 0 ? labels[i - w] : 0;
                if (left && up) { labels[i] = Math.min(left, up); union(left, up); }
                else if (left) labels[i] = left;
                else if (up) labels[i] = up;
                else { labels[i] = nextLabel; parent[nextLabel] = nextLabel; nextLabel++; }
            }
        }
        const sizes = new Map();
        const bbox = new Map();
        for (let y = 0; y < h; y++) {
            for (let x = 0; x < w; x++) {
                const i = y * w + x;
                if (!labels[i]) continue;
                const root = find(labels[i]);
                sizes.set(root, (sizes.get(root) || 0) + 1);
                const bb = bbox.get(root) || { minX: x, maxX: x, minY: y, maxY: y };
                if (x < bb.minX) bb.minX = x;
                if (x > bb.maxX) bb.maxX = x;
                if (y < bb.minY) bb.minY = y;
                if (y > bb.maxY) bb.maxY = y;
                bbox.set(root, bb);
            }
        }

        let bestRoot = 0;
        let bestScore = 0;
        sizes.forEach((size, root) => {
            if (size < total * 0.08) return;
            const bb = bbox.get(root);
            const bw = bb.maxX - bb.minX;
            const bh = bb.maxY - bb.minY;
            if (bw < w * 0.25 || bh < h * 0.25) return;
            const touchesAll =
                bb.minX <= 1 && bb.maxX >= w - 2 &&
                bb.minY <= 1 && bb.maxY >= h - 2;
            let score = size;
            if (touchesAll) score *= 0.35;
            if (score > bestScore) { bestScore = score; bestRoot = root; }
        });

        if (!bestRoot) {
            return inset(naturalW, naturalH);
        }

        const points = [];
        for (let y = 0; y < h; y++) {
            for (let x = 0; x < w; x++) {
                const i = y * w + x;
                if (!labels[i] || find(labels[i]) !== bestRoot) continue;
                const isBoundary =
                    x === 0 || y === 0 || x === w - 1 || y === h - 1 ||
                    find(labels[i - 1] || 0) !== bestRoot ||
                    find(labels[i + 1] || 0) !== bestRoot ||
                    find(labels[i - w] || 0) !== bestRoot ||
                    find(labels[i + w] || 0) !== bestRoot;
                if (isBoundary) points.push({ x, y });
            }
        }
        if (points.length < 30) {
            return inset(naturalW, naturalH);
        }
        let tl = points[0], tr = points[0], br = points[0], bl = points[0];
        for (const p of points) {
            if (p.x + p.y < tl.x + tl.y) tl = p;
            if (p.x - p.y > tr.x - tr.y) tr = p;
            if (p.x + p.y > br.x + br.y) br = p;
            if (p.y - p.x > bl.y - bl.x) bl = p;
        }
        const sx = naturalW / w;
        const sy = naturalH / h;
        const corners = {
            tl: { x: tl.x * sx, y: tl.y * sy },
            tr: { x: tr.x * sx, y: tr.y * sy },
            br: { x: br.x * sx, y: br.y * sy },
            bl: { x: bl.x * sx, y: bl.y * sy }
        };
        if (quadArea(corners) < naturalW * naturalH * 0.1) {
            return inset(naturalW, naturalH);
        }
        return corners;
    }

    function quadArea(c) {
        const pts = [c.tl, c.tr, c.br, c.bl];
        let area = 0;
        for (let i = 0; i < 4; i++) {
            const a = pts[i];
            const b = pts[(i + 1) % 4];
            area += a.x * b.y - b.x * a.y;
        }
        return Math.abs(area) / 2;
    }

    function inset(w, h) {
        const padX = w * 0.1, padY = h * 0.1;
        return {
            tl: { x: padX, y: padY },
            tr: { x: w - padX, y: padY },
            br: { x: w - padX, y: h - padY },
            bl: { x: padX, y: h - padY }
        };
    }

    function processImage(params) {
        const rawDataUrl = params.dataUrl;
        const corners = params.corners;
        const rotation = params.rotation || 0;
        const autoEnhance = params.autoEnhance !== false;
        const filter = params.filter || 'color';

        return new Promise(resolve => {
            if (!rawDataUrl || !corners) { resolve(''); return; }
            const tmp = new Image();
            tmp.onload = () => {
                const dstWidth = Math.max(distance(corners.tl, corners.tr), distance(corners.bl, corners.br));
                const dstHeight = Math.max(distance(corners.tl, corners.bl), distance(corners.tr, corners.br));
                const dst = {
                    tl: { x: 0, y: 0 },
                    tr: { x: dstWidth, y: 0 },
                    br: { x: dstWidth, y: dstHeight },
                    bl: { x: 0, y: dstHeight }
                };
                const source = document.createElement('canvas');
                source.width = tmp.naturalWidth;
                source.height = tmp.naturalHeight;
                source.getContext('2d').drawImage(tmp, 0, 0);
                const sourceData = source.getContext('2d').getImageData(0, 0, source.width, source.height);

                const warp = document.createElement('canvas');
                warp.width = Math.round(dstWidth);
                warp.height = Math.round(dstHeight);
                const wctx = warp.getContext('2d');
                const out = wctx.createImageData(warp.width, warp.height);
                const transform = computePerspectiveTransform(corners, dst);
                if (!transform) { resolve(''); return; }
                const inv = invert3x3(transform);
                if (!inv) { resolve(''); return; }
                for (let y = 0; y < warp.height; y++) {
                    for (let x = 0; x < warp.width; x++) {
                        const srcPt = applyTransform(inv, x, y);
                        const color = sampleBilinear(sourceData, source.width, source.height, srcPt.x, srcPt.y);
                        const offset = (y * warp.width + x) * 4;
                        out.data[offset] = color.r;
                        out.data[offset + 1] = color.g;
                        out.data[offset + 2] = color.b;
                        out.data[offset + 3] = 255;
                    }
                }
                if (autoEnhance) enhance(out);
                applyFilter(out, filter);
                wctx.putImageData(out, 0, 0);

                let finalCanvas = warp;
                if (rotation && rotation % 360 !== 0) {
                    finalCanvas = rotateCanvas(warp, rotation);
                }
                resolve(finalCanvas.toDataURL('image/png'));
            };
            tmp.src = rawDataUrl;
        });
    }

    function rotateCanvas(canvas, degrees) {
        const rad = (degrees % 360) * Math.PI / 180;
        const swap = degrees % 180 !== 0;
        const w = swap ? canvas.height : canvas.width;
        const h = swap ? canvas.width : canvas.height;
        const out = document.createElement('canvas');
        out.width = w;
        out.height = h;
        const ctx = out.getContext('2d');
        ctx.translate(w / 2, h / 2);
        ctx.rotate(rad);
        ctx.drawImage(canvas, -canvas.width / 2, -canvas.height / 2);
        return out;
    }

    function enhance(imageData) {
        const data = imageData.data;
        const w = imageData.width;
        const h = imageData.height;
        const pixelCount = w * h;
        const histR = new Array(256).fill(0);
        const histG = new Array(256).fill(0);
        const histB = new Array(256).fill(0);
        for (let i = 0; i < pixelCount; i++) {
            const o = i * 4;
            histR[data[o]]++;
            histG[data[o + 1]]++;
            histB[data[o + 2]]++;
        }
        const loR = percentile(histR, pixelCount, 0.02);
        const hiR = percentile(histR, pixelCount, 0.98);
        const loG = percentile(histG, pixelCount, 0.02);
        const hiG = percentile(histG, pixelCount, 0.98);
        const loB = percentile(histB, pixelCount, 0.02);
        const hiB = percentile(histB, pixelCount, 0.98);
        const rngR = Math.max(10, hiR - loR);
        const rngG = Math.max(10, hiG - loG);
        const rngB = Math.max(10, hiB - loB);

        let meanR = 0, meanG = 0, meanB = 0;
        for (let i = 0; i < pixelCount; i++) {
            const o = i * 4;
            meanR += data[o]; meanG += data[o + 1]; meanB += data[o + 2];
        }
        meanR /= pixelCount; meanG /= pixelCount; meanB /= pixelCount;
        const gray = (meanR + meanG + meanB) / 3;
        const wbR = gray / Math.max(1, meanR);
        const wbG = gray / Math.max(1, meanG);
        const wbB = gray / Math.max(1, meanB);

        const stretched = new Uint8ClampedArray(data.length);
        for (let i = 0; i < pixelCount; i++) {
            const o = i * 4;
            stretched[o]     = clamp255(((data[o]     - loR) * 255 / rngR) * wbR);
            stretched[o + 1] = clamp255(((data[o + 1] - loG) * 255 / rngG) * wbG);
            stretched[o + 2] = clamp255(((data[o + 2] - loB) * 255 / rngB) * wbB);
            stretched[o + 3] = 255;
        }

        const amount = 0.55;
        for (let y = 0; y < h; y++) {
            for (let x = 0; x < w; x++) {
                const o = (y * w + x) * 4;
                for (let c = 0; c < 3; c++) {
                    const orig = stretched[o + c];
                    let sum = 0, count = 0;
                    for (let dy = -1; dy <= 1; dy++) {
                        for (let dx = -1; dx <= 1; dx++) {
                            const ny = y + dy, nx = x + dx;
                            if (ny < 0 || ny >= h || nx < 0 || nx >= w) continue;
                            sum += stretched[(ny * w + nx) * 4 + c];
                            count++;
                        }
                    }
                    const blurv = sum / count;
                    data[o + c] = clamp255(orig + amount * (orig - blurv));
                }
                data[o + 3] = 255;
            }
        }
    }

    function applyFilter(imageData, filter) {
        if (filter === 'color') return;
        const data = imageData.data;
        const w = imageData.width;
        const h = imageData.height;
        const pixelCount = w * h;
        if (filter === 'gray') {
            for (let i = 0; i < pixelCount; i++) {
                const o = i * 4;
                const lum = data[o] * 0.299 + data[o + 1] * 0.587 + data[o + 2] * 0.114;
                data[o] = data[o + 1] = data[o + 2] = clamp255(lum);
            }
            return;
        }

        const gray = new Uint8ClampedArray(pixelCount);
        for (let i = 0; i < pixelCount; i++) {
            const o = i * 4;
            gray[i] = (data[o] * 0.299 + data[o + 1] * 0.587 + data[o + 2] * 0.114) | 0;
        }
        const sat = new Float64Array(pixelCount);
        for (let y = 0; y < h; y++) {
            let rowSum = 0;
            for (let x = 0; x < w; x++) {
                rowSum += gray[y * w + x];
                sat[y * w + x] = (y > 0 ? sat[(y - 1) * w + x] : 0) + rowSum;
            }
        }
        const window = Math.max(7, Math.floor(Math.min(w, h) / 32));
        const bias = 8;
        for (let y = 0; y < h; y++) {
            for (let x = 0; x < w; x++) {
                const x1 = Math.max(0, x - window);
                const y1 = Math.max(0, y - window);
                const x2 = Math.min(w - 1, x + window);
                const y2 = Math.min(h - 1, y + window);
                const A = (y1 > 0 && x1 > 0) ? sat[(y1 - 1) * w + (x1 - 1)] : 0;
                const B = (y1 > 0) ? sat[(y1 - 1) * w + x2] : 0;
                const C = (x1 > 0) ? sat[y2 * w + (x1 - 1)] : 0;
                const D = sat[y2 * w + x2];
                const area = (x2 - x1 + 1) * (y2 - y1 + 1);
                const mean = (D - B - C + A) / area;
                const o = (y * w + x) * 4;
                const v = gray[y * w + x] >= (mean - bias) ? 255 : 0;
                data[o] = data[o + 1] = data[o + 2] = v;
            }
        }
    }

    function clamp255(v) { return v < 0 ? 0 : v > 255 ? 255 : v; }

    function percentile(hist, total, p) {
        const target = total * p;
        let acc = 0;
        for (let i = 0; i < 256; i++) {
            acc += hist[i];
            if (acc >= target) return i;
        }
        return 255;
    }

    function distance(a, b) {
        const dx = a.x - b.x, dy = a.y - b.y;
        return Math.sqrt(dx * dx + dy * dy);
    }

    function computePerspectiveTransform(src, dst) {
        const matrix = [
            [src.tl.x, src.tl.y, 1, 0, 0, 0, -dst.tl.x * src.tl.x, -dst.tl.x * src.tl.y],
            [0, 0, 0, src.tl.x, src.tl.y, 1, -dst.tl.y * src.tl.x, -dst.tl.y * src.tl.y],
            [src.tr.x, src.tr.y, 1, 0, 0, 0, -dst.tr.x * src.tr.x, -dst.tr.x * src.tr.y],
            [0, 0, 0, src.tr.x, src.tr.y, 1, -dst.tr.y * src.tr.x, -dst.tr.y * src.tr.y],
            [src.br.x, src.br.y, 1, 0, 0, 0, -dst.br.x * src.br.x, -dst.br.x * src.br.y],
            [0, 0, 0, src.br.x, src.br.y, 1, -dst.br.y * src.br.x, -dst.br.y * src.br.y],
            [src.bl.x, src.bl.y, 1, 0, 0, 0, -dst.bl.x * src.bl.x, -dst.bl.x * src.bl.y],
            [0, 0, 0, src.bl.x, src.bl.y, 1, -dst.bl.y * src.bl.x, -dst.bl.y * src.bl.y]
        ];
        const vector = [dst.tl.x, dst.tl.y, dst.tr.x, dst.tr.y, dst.br.x, dst.br.y, dst.bl.x, dst.bl.y];
        const solution = solveLinearSystem(matrix, vector);
        if (!solution) return null;
        return [
            [solution[0], solution[1], solution[2]],
            [solution[3], solution[4], solution[5]],
            [solution[6], solution[7], 1]
        ];
    }

    function solveLinearSystem(matrix, vector) {
        const size = vector.length;
        const m = matrix.map((row, i) => [...row, vector[i]]);
        for (let i = 0; i < size; i++) {
            let maxRow = i;
            for (let k = i + 1; k < size; k++) {
                if (Math.abs(m[k][i]) > Math.abs(m[maxRow][i])) maxRow = k;
            }
            if (Math.abs(m[maxRow][i]) < 1e-8) return null;
            if (maxRow !== i) { const tmp = m[i]; m[i] = m[maxRow]; m[maxRow] = tmp; }
            const pivot = m[i][i];
            for (let j = i; j <= size; j++) m[i][j] /= pivot;
            for (let k = 0; k < size; k++) {
                if (k === i) continue;
                const factor = m[k][i];
                for (let j = i; j <= size; j++) m[k][j] -= factor * m[i][j];
            }
        }
        return m.map(row => row[size]);
    }

    function invert3x3(matrix) {
        const m = matrix;
        const a = m[0][0], b = m[0][1], c = m[0][2];
        const d = m[1][0], e = m[1][1], f = m[1][2];
        const g = m[2][0], h = m[2][1], i = m[2][2];
        const det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
        if (Math.abs(det) < 1e-8) return null;
        const invDet = 1 / det;
        return [
            [(e * i - f * h) * invDet, (c * h - b * i) * invDet, (b * f - c * e) * invDet],
            [(f * g - d * i) * invDet, (a * i - c * g) * invDet, (c * d - a * f) * invDet],
            [(d * h - e * g) * invDet, (b * g - a * h) * invDet, (a * e - b * d) * invDet]
        ];
    }

    function applyTransform(matrix, x, y) {
        const denom = matrix[2][0] * x + matrix[2][1] * y + matrix[2][2];
        const nx = (matrix[0][0] * x + matrix[0][1] * y + matrix[0][2]) / denom;
        const ny = (matrix[1][0] * x + matrix[1][1] * y + matrix[1][2]) / denom;
        return { x: nx, y: ny };
    }

    function sampleBilinear(imageData, width, height, x, y) {
        const xi = Math.max(0, Math.min(width - 1, x));
        const yi = Math.max(0, Math.min(height - 1, y));
        const x0 = Math.floor(xi), y0 = Math.floor(yi);
        const x1 = Math.min(width - 1, x0 + 1), y1 = Math.min(height - 1, y0 + 1);
        const dx = xi - x0, dy = yi - y0;
        const idx00 = (y0 * width + x0) * 4;
        const idx10 = (y0 * width + x1) * 4;
        const idx01 = (y1 * width + x0) * 4;
        const idx11 = (y1 * width + x1) * 4;
        const r = lerp(lerp(imageData.data[idx00], imageData.data[idx10], dx),
            lerp(imageData.data[idx01], imageData.data[idx11], dx), dy);
        const g = lerp(lerp(imageData.data[idx00 + 1], imageData.data[idx10 + 1], dx),
            lerp(imageData.data[idx01 + 1], imageData.data[idx11 + 1], dx), dy);
        const b = lerp(lerp(imageData.data[idx00 + 2], imageData.data[idx10 + 2], dx),
            lerp(imageData.data[idx01 + 2], imageData.data[idx11 + 2], dx), dy);
        return { r, g, b };
    }

    function lerp(a, b, t) { return a + (b - a) * t; }

    window.JScanner = {
        detectQuad,
        processImage
    };
})();
