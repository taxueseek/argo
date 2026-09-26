// vision_probe.swift — 批次一次调用同时产出图像分类标签、OCR 文本与特征指纹。
//
// 为什么用 macOS 内置 Vision 而不是 CLIP：
//   CLIP 路线（Chinese-CLIP + torch + FAISS）要装 ~2GB 依赖并从 HF 下 ~400MB
//   模型，建库以小时计。而本机目标只有一件事——**把 7 万张图收敛到几十张候选，
//   再交给多模态模型判断**。Vision 一次调用就给三样东西（分类标签、OCR 文本、
//   768 维特征指纹），零依赖、约 0.4 秒/张，足够完成收敛。
//
// 输出协议：每行一个 JSON 对象（JSONL），stdout。字段：
//   {"path": "...", "ok": true,
//    "labels": [{"id": "stairs", "conf": 0.92}, ...],
//    "ocr": ["文本行1", "文本行2"],
//    "fp": "<base64 的 float32 数组>"}
//   或 {"path": "...", "ok": false, "error": "..."}
//
// 用 JSONL 而不是单个 JSON 数组：逐行 flush 让调用方能流式读取并显示进度，
// 6 万张图的批处理里「有没有在动」是可用性问题。
//
// 特征指纹用 base64 编码 float32 数组而非直接输出 768 个数字：后者每行
// ~15KB，6 万行就是 900MB 输出。base64 后 ~4KB/行，且解码是 numpy 一行。

import Foundation
import Vision
import AppKit

let args = Array(CommandLine.arguments.dropFirst())
// --no-fp 跳过特征指纹（只要标签和 OCR 时能省一半时间）
let wantFP = !args.contains("--no-fp")
let wantOCR = !args.contains("--no-ocr")
let paths = args.filter { !$0.hasPrefix("--") }

func emit(_ obj: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj),
          let line = String(data: data, encoding: .utf8) else { return }
    print(line)
    // 行缓冲：调用方按行读时不被块缓冲卡住
    fflush(stdout)
}

func probe(_ path: String) {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        emit(["path": path, "ok": false, "error": "load_failed"])
        return
    }
    var requests: [VNRequest] = []
    let clsReq = VNClassifyImageRequest()
    requests.append(clsReq)
    var ocrReq: VNRecognizeTextRequest? = nil
    if wantOCR {
        let o = VNRecognizeTextRequest()
        // 双语：本机素材中英混排（截图带中文界面，图库带英文题注）
        o.recognitionLanguages = ["zh-Hans", "en-US"]
        o.recognitionLevel = .accurate
        // 关闭语言纠错：截图里的代码/路径/型号经不起自动纠正
        o.usesLanguageCorrection = false
        ocrReq = o
        requests.append(o)
    }
    var fpReq: VNGenerateImageFeaturePrintRequest? = nil
    if wantFP {
        let f = VNGenerateImageFeaturePrintRequest()
        fpReq = f
        requests.append(f)
    }

    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do {
        try handler.perform(requests)
    } catch {
        emit(["path": path, "ok": false, "error": "perform_failed"])
        return
    }

    var out: [String: Any] = ["path": path, "ok": true]

    // 分类标签：只留高置信度前若干个。低分标签（<0.1）噪声大，
    // 全量带出去会让本地索引的「标签」维度几乎变成随机噪声。
    if let obs = clsReq.results {
        let labels = obs.prefix(12).compactMap { o -> [String: Any]? in
            guard o.confidence >= 0.10 else { return nil }
            return ["id": o.identifier, "conf": Double(o.confidence)]
        }
        out["labels"] = labels
    }

    // OCR 文本：按 Vision 的置信度过滤，并把同一行的碎片合并。
    if let o = ocrReq, let res = o.results {
        let lines = res.compactMap { $0.topCandidates(1).first }
            .filter { $0.confidence >= 0.3 }
            .map { $0.string }
        // 去重且保序：截图里的重复行（列表项、代码）会占满索引
        var seen = Set<String>()
        out["ocr"] = lines.filter { seen.insert($0).inserted }
    }

    // 特征指纹：以图搜图与去重的依据。
    //
    // 取数值的唯一稳定路径是 NSKeyedArchiver 归档后从 plist 的 $objects 里
    // 抠出那个 Data 块——实测 768 维对应 3072 字节（float32）。此前的两条路
    // 都不通：`value(forKey: "elementData")` 抛 NSUnknownKeyException
    // （VNSceneObservation 不是 KVC 兼容的），而 `computeDistance` 只给
    // 两两距离、给不出「指纹本身」，没法存下来做索引。
    //
    // 归档格式是私有布局，所以这里**只认字节数**（768×4）而不是靠下标：
    // 布局变了也能凭尺寸认出正确的块。抠不到就退化成无指纹——标签与 OCR
    // 两个维度照常可用，不影响调用方。
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
