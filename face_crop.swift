// Finds faces in photos and cuts square pictures out of them, for google_photos.py.
// macOS only (Vision, ImageIO). Run through `swift face_crop.swift`:
//
//   swift face_crop.swift faces a.jpg b.jpg   -> {"a.jpg": {"width":W,"height":H,"faces":[[x,y,w,h],..]}, ..}
//   swift face_crop.swift crop < jobs.json    jobs: [{"src","dst","x","y","side","size"}]
//
// All boxes are in pixels of the upright picture, origin top left.
import CoreGraphics
import Foundation
import ImageIO
import Vision

func load(_ path: String) -> CGImage? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else { return nil }
    // The thumbnail call is the one that turns the picture upright from its EXIF orientation.
    let opts: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                                 kCGImageSourceCreateThumbnailWithTransform: true,
                                 kCGImageSourceThumbnailMaxPixelSize: 4096]
    return CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary)
}

func faces(_ paths: [String]) throws {
    var out: [String: Any] = [:]
    for path in paths {
        guard let image = load(path) else { continue }
        let w = Double(image.width), h = Double(image.height)
        let request = VNDetectFaceRectanglesRequest()
        try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        let boxes = (request.results ?? []).map { face -> [Double] in
            let b = face.boundingBox  // normalized, origin bottom left
            return [b.minX * w, (1 - b.maxY) * h, b.width * w, b.height * h]
        }
        out[path] = ["width": image.width, "height": image.height, "faces": boxes]
    }
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: out))
}

func crop() throws {
    let jobs = try JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as? [[String: Any]] ?? []
    for job in jobs {
        guard let src = job["src"] as? String, let dst = job["dst"] as? String,
              let x = job["x"] as? Int, let y = job["y"] as? Int,
              let side = job["side"] as? Int, let size = job["size"] as? Int,
              let image = load(src),
              let part = image.cropping(to: CGRect(x: x, y: y, width: side, height: side)),
              let ctx = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                  bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
        else { continue }
        ctx.interpolationQuality = .high
        ctx.draw(part, in: CGRect(x: 0, y: 0, width: size, height: size))
        guard let small = ctx.makeImage(),
              let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath: dst) as CFURL, "public.jpeg" as CFString, 1, nil)
        else { continue }
        CGImageDestinationAddImage(dest, small, [kCGImageDestinationLossyCompressionQuality: 0.9] as CFDictionary)
        CGImageDestinationFinalize(dest)
    }
}

let args = Array(CommandLine.arguments.dropFirst())
do {
    switch args.first {
    case "faces": try faces(Array(args.dropFirst()))
    case "crop": try crop()
    default:
        FileHandle.standardError.write("usage: face_crop.swift faces <image>... | crop < jobs.json\n".data(using: .utf8)!)
        exit(2)
    }
} catch {
    FileHandle.standardError.write("\(error)\n".data(using: .utf8)!)
    exit(1)
}
