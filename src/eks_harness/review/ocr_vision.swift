import AppKit
import Foundation
import Vision

var languages = ["tr-TR", "en-US"]
var files: [String] = []
var arguments = Array(CommandLine.arguments.dropFirst())
while !arguments.isEmpty {
    let item = arguments.removeFirst()
    if item == "--lang", !arguments.isEmpty {
        languages = arguments.removeFirst().split(separator: ",").map(String.init)
    } else {
        files.append(item)
    }
}

for file in files {
    var lines: [[String: Any]] = []
    if let image = NSImage(contentsOfFile: file),
       let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        request.recognitionLanguages = languages
        let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
        try? handler.perform([request])
        for observation in request.results ?? [] {
            guard let top = observation.topCandidates(1).first else { continue }
            let box = observation.boundingBox
            lines.append([
                "text": top.string,
                "confidence": Double(top.confidence),
                "box": [Double(box.minX), Double(1 - box.maxY), Double(box.width), Double(box.height)],
            ])
        }
    }
    let payload: [String: Any] = ["file": file, "lines": lines]
    if let data = try? JSONSerialization.data(withJSONObject: payload),
       let text = String(data: data, encoding: .utf8) {
        print(text)
    }
}
