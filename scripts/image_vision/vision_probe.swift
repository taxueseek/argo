// vision_probe.swift — 批次一次调用同时产出图像分类标签、OCR 文本、原始尺寸与特征指纹。
//
// 性能优化（v2）：
//   1. ImageIO 缩图解码（CGImageSourceCreateThumbnailAtIndex）：替代 NSImage 全像素解码，
//      同时零解码读取原图 width/height，消除 5K 截图与大 PNG 的内存与解码开销。
//   2. 分流 OCR（--ocr-mode auto|accurate|fast|off，默认 auto）：
//      先执行分类 + 指纹，命中文档/截图/文字类标签或截图命名时跑 .accurate 双语 OCR，
//      其余常规照片/素材降级为 .fast OCR，兼顾速度与文字兜底召回。
//
// 输出协议：每行一个 JSON 对象（JSONL），stdout。字段：
//   {"path": "...", "ok": true, "width": 1920, "height": 1080,
//    "labels": [{"id": "stairs", "conf": 0.92}, ...],
//    "ocr": ["文本行1", "文本行2"],
//    "fp": "<base64 的 float32 数组>", "fp_dim": 768}
//   或 {"path": "...", "ok": false, "error": "..."}

import Foundation
import Vision
import ImageIO

let rawArgs = Array(CommandLine.arguments.dropFirst())
var wantFP = true
var ocrMode = "auto"  // auto | accurate | fast | off
var maxDim = 1280
var paths: [String] = []

var idx = 0
while idx < rawArgs.count {
    let a = rawArgs[idx]
    if a == "--no-fp" {
        wantFP = false
    } else if a == "--no-ocr" {
        ocrMode = "off"
    } else if a == "--fast-ocr" {
        ocrMode = "fast"
    } else if a == "--ocr-mode", idx + 1 < rawArgs.count {
        idx += 1
        ocrMode = rawArgs[idx].lowercased()
    } else if a == "--max-dim", idx + 1 < rawArgs.count {
        idx += 1
        if let v = Int(rawArgs[idx]), v >= 256 {
            maxDim = v
        }
    } else if !a.hasPrefix("--") {
        paths.append(a)
    }
    idx += 1
}

// 触发精准 OCR 的分类标签子串与路径线索
let textLabelHints = [
    "document", "text", "screenshot", "screen", "monitor", "computer",
    "whiteboard", "blackboard", "poster", "sign", "menu", "book", "page",
    "receipt", "newspaper", "magazine", "typography", "diagram", "chart",
    "graph", "webpage", "presentation", "slide", "paper", "card", "letter",
    "invoice", "envelope", "label", "banner", "billboard", "note",
]
let textPathHints = [
    "screenshot", "screen shot", "截屏", "截图", "微信", "wechat",
    "ppt", "slide", "doc", "invoice", "发票", "报表", "账单", "白板",
]

func emit(_ obj: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj),
          let line = String(data: data, encoding: .utf8) else { return }
    print(line)
    fflush(stdout)
}

func loadDownsampled(_ path: String, maxPixel: Int) -> (CGImage, Int, Int)? {
    let url = URL(fileURLWithPath: path)
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil) else {
        return nil
    }
    var origW = 0
    var origH = 0
    if let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any] {
        origW = (props[kCGImagePropertyPixelWidth] as? Int) ?? 0
        origH = (props[kCGImagePropertyPixelHeight] as? Int) ?? 0
    }
    let opts: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceShouldCacheImmediately: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: maxPixel,
    ]
    if let thumb = CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary) {
        return (thumb, origW > 0 ? origW : thumb.width, origH > 0 ? origH : thumb.height)
    }
    if let full = CGImageSourceCreateImageAtIndex(src, 0, nil) {
        return (full, origW > 0 ? origW : full.width, origH > 0 ? origH : full.height)
    }
    return nil
}

func shouldUseAccurateOCR(path: String, labels: [[String: Any]]) -> Bool {
    let lowerPath = path.lowercased()
    if lowerPath.hasSuffix(".png") {
        // 截图与设计文字稿多为 PNG；路径命中截图词或含文字标签直接走精准 OCR
        for h in textPathHints {
            if lowerPath.contains(h) { return true }
        }
    }
    for h in textPathHints {
        if lowerPath.contains(h) { return true }
    }
    for item in labels {
        guard let lid = (item["id"] as? String)?.lowercased(),
              let conf = item["conf"] as? Double, conf >= 0.15 else { continue }
        for h in textLabelHints {
            if lid.contains(h) { return true }
        }
    }
    return false
}

func probe(_ path: String) {
    guard let (cg, origW, origH) = loadDownsampled(path, maxPixel: maxDim) else {
        emit(["path": path, "ok": false, "error": "load_failed"])
        return
    }

    let clsReq = VNClassifyImageRequest()
    var firstBatch: [VNRequest] = [clsReq]
    var fpReq: VNGenerateImageFeaturePrintRequest? = nil
    if wantFP {
        let f = VNGenerateImageFeaturePrintRequest()
        fpReq = f
        firstBatch.append(f)
    }

    // 非 auto 模式可直接合并 OCR 到同一次 perform，减少调度开销
    var ocrReq: VNRecognizeTextRequest? = nil
    if ocrMode == "accurate" || ocrMode == "fast" {
        let o = VNRecognizeTextRequest()
        o.recognitionLanguages = ["zh-Hans", "en-US"]
        o.recognitionLevel = (ocrMode == "accurate") ? .accurate : .fast
        o.usesLanguageCorrection = false
        ocrReq = o
        firstBatch.append(o)
    }

    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do {
        try handler.perform(firstBatch)
    } catch {
        emit(["path": path, "ok": false, "error": "perform_failed"])
        return
    }

    var out: [String: Any] = [
        "path": path,
        "ok": true,
        "width": origW,
        "height": origH,
    ]

    var labelList: [[String: Any]] = []
    if let obs = clsReq.results {
        labelList = obs.prefix(12).compactMap { o -> [String: Any]? in
            guard o.confidence >= 0.10 else { return nil }
            return ["id": o.identifier, "conf": Double(o.confidence)]
        }
        out["labels"] = labelList
    }

    // auto 模式：根据分类标签与路径特征分流 OCR 档位
    var actualOCRMode = ocrMode
    if ocrMode == "auto" {
        let accurate = shouldUseAccurateOCR(path: path, labels: labelList)
        actualOCRMode = accurate ? "accurate" : "fast"
        let o = VNRecognizeTextRequest()
        o.recognitionLanguages = ["zh-Hans", "en-US"]
        o.recognitionLevel = accurate ? .accurate : .fast
        o.usesLanguageCorrection = false
        do {
            try handler.perform([o])
            ocrReq = o
        } catch {
            // OCR 失败不影响标签与指纹输出
        }
    }
    out["ocr_mode"] = actualOCRMode

    if let o = ocrReq, let res = o.results {
        let lines = res.compactMap { $0.topCandidates(1).first }
            .filter { $0.confidence >= 0.3 }
            .map { $0.string }
        var seen = Set<String>()
        out["ocr"] = lines.filter { seen.insert($0).inserted }
    }

    if let f = fpReq, let obs = f.results?.first as? VNFeaturePrintObservation {
        let want = obs.elementCount * 4
        if let arch = try? NSKeyedArchiver.archivedData(
                withRootObject: obs, requiringSecureCoding: false),
           let plist = try? PropertyListSerialization.propertyList(
                from: arch, format: nil) as? [String: Any],
           let objects = plist["$objects"] as? [Any] {
            let blob = objects.compactMap { $0 as? Data }
                .first { $0.count == want }
            if let blob {
                out["fp"] = blob.base64EncodedString()
                out["fp_dim"] = obs.elementCount
            }
        }
    }
    emit(out)
}

for p in paths { probe(p) }
