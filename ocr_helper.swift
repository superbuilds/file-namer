// file-namer helper. Reads one absolute path per line on stdin and writes one JSON object per line on stdout.
// One file at a time, one long-lived process, everything on-device.
//   images -> Vision OCR (text plus line position/height, used to pick a title)
//   pdf    -> PDFKit text of the first pages (+ document title)
// Build: swiftc -O ocr_helper.swift -o ocr_helper
import Foundation
import Vision
import AppKit
import PDFKit

setvbuf(stdout, nil, _IOLBF, 0)
let maxPdfChars = 4000

func emit(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) { print(s) }
}

func ocrImage(_ path: String) -> [String: Any] {
    var rec: [String: Any] = ["path": path, "mode": "ocr"]
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let cg = CGImageSourceCreateImageAtIndex(src, 0, nil) else { rec["error"] = "load"; return rec }
    rec["w"] = cg.width; rec["h"] = cg.height
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = true
    do { try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req]) } catch { rec["error"] = "ocr"; return rec }
    var lines: [[String: Any]] = []
    for o in req.results ?? [] {
        guard let c = o.topCandidates(1).first else { continue }
        let b = o.boundingBox
        lines.append(["t": c.string, "c": Double(c.confidence),
                      "x": Double(b.origin.x), "y": Double(1 - b.origin.y - b.height),
                      "w": Double(b.width), "h": Double(b.height)])
    }
    rec["lines"] = lines
    return rec
}

func pdfText(_ path: String) -> [String: Any] {
    var rec: [String: Any] = ["path": path, "mode": "pdf"]
    guard let doc = PDFDocument(url: URL(fileURLWithPath: path)) else { rec["error"] = "load"; return rec }
    rec["pages"] = doc.pageCount
    if let attrs = doc.documentAttributes, let t = attrs[PDFDocumentAttribute.titleAttribute] as? String { rec["title"] = t }
    var text = ""
    var i = 0
    while i < min(doc.pageCount, 3) && text.count < maxPdfChars {
        text += (doc.page(at: i)?.string ?? "") + "\n"; i += 1
    }
    rec["text"] = String(text.prefix(maxPdfChars))
    return rec
}

while let line = readLine() {
    let p = line.trimmingCharacters(in: .whitespacesAndNewlines)
    if p.isEmpty { continue }
    autoreleasepool {
        emit(p.lowercased().hasSuffix(".pdf") ? pdfText(p) : ocrImage(p))
    }
}
