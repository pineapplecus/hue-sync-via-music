// hue_stream: DTLS-PSK streamer for the Hue Entertainment API (HueStream v2, RGB).
//
// usage: hue_stream <host> <port> <psk-identity> <psk-hex> <area-uuid> <channels> <fps>
//
// stdin : repeated frames of <channels> * 3 bytes (r, g, b as 0-255 for channel ids 0..n-1).
//         Only the NEWEST frame matters: it is re-sent at <fps> so the bridge never starves,
//         and nothing queues up. If the producer is slow, old frames are simply replaced.
// stdout: "READY" once the DTLS handshake completes.
// stderr: errors. Exit code 2 = connection failed, 3 = handshake timeout.

import Foundation
import Network
import Security

setvbuf(stdout, nil, _IOLBF, 0)

let argv = CommandLine.arguments
guard argv.count == 8,
      let port = NWEndpoint.Port(argv[2]),
      let channels = Int(argv[6]), channels > 0,
      let fps = Double(argv[7]), fps > 0 else {
    fputs("usage: hue_stream <host> <port> <psk-identity> <psk-hex> <area-uuid> <channels> <fps>\n", stderr)
    exit(64)
}
let host = argv[1], identity = argv[3], areaID = argv[5]

func hexBytes(_ s: String) -> [UInt8]? {
    guard s.count % 2 == 0 else { return nil }
    var out: [UInt8] = []
    var i = s.startIndex
    while i < s.endIndex {
        let j = s.index(i, offsetBy: 2)
        guard let b = UInt8(s[i..<j], radix: 16) else { return nil }
        out.append(b)
        i = j
    }
    return out
}
guard let psk = hexBytes(argv[4]) else { fputs("bad psk (expected hex)\n", stderr); exit(64) }
guard areaID.utf8.count == 36 else { fputs("area id must be a 36-char UUID\n", stderr); exit(64) }

// ---- DTLS 1.2 + TLS_PSK_WITH_AES_128_GCM_SHA256 (what the bridge speaks on UDP 2100)
let tls = NWProtocolTLS.Options()
let sec = tls.securityProtocolOptions
let idBytes = Array(identity.utf8)
psk.withUnsafeBytes { p in
    idBytes.withUnsafeBytes { q in
        sec_protocol_options_add_pre_shared_key(sec, DispatchData(bytes: p) as __DispatchData,
                                                DispatchData(bytes: q) as __DispatchData)
    }
}
sec_protocol_options_append_tls_ciphersuite(sec, tls_ciphersuite_t(rawValue: 0x00A8)!)  // PSK_AES128_GCM_SHA256
sec_protocol_options_set_min_tls_protocol_version(sec, .DTLSv12)
sec_protocol_options_set_max_tls_protocol_version(sec, .DTLSv12)
let params = NWParameters(dtls: tls, udp: NWProtocolUDP.Options())

// ---- latest-frame-wins state, fed by a stdin reader thread
let lock = NSLock()
var latest = [UInt8](repeating: 0, count: channels * 3)
var haveFrame = false
var eof = false

let reader = Thread {
    let frameSize = channels * 3
    var buf = [UInt8](repeating: 0, count: 4096)
    var acc: [UInt8] = []
    while true {
        let n = read(0, &buf, buf.count)
        if n <= 0 { break }
        acc.append(contentsOf: buf[0..<n])
        if acc.count >= frameSize {
            let whole = (acc.count / frameSize) * frameSize
            let frame = Array(acc[(whole - frameSize)..<whole])   // keep only the newest complete frame
            acc.removeFirst(whole)
            lock.lock(); latest = frame; haveFrame = true; lock.unlock()
        }
    }
    lock.lock(); eof = true; lock.unlock()
}

// ---- HueStream v2 packet: 16-byte header, 36-byte area id, then 7 bytes per channel
var seq: UInt8 = 0
func packet(_ rgb: [UInt8]) -> Data {
    var d = Data("HueStream".utf8)
    d.append(contentsOf: [0x02, 0x00])      // API version 2.0
    d.append(seq); seq &+= 1                // sequence id
    d.append(contentsOf: [0x00, 0x00])      // reserved
    d.append(0x00)                          // color space: RGB
    d.append(0x00)                          // reserved
    d.append(contentsOf: Array(areaID.utf8))
    for ch in 0..<channels {
        d.append(UInt8(ch))
        for c in 0..<3 {
            let v = UInt16(rgb[ch * 3 + c]) * 257   // 0-255 -> 0-65535
            d.append(UInt8(v >> 8)); d.append(UInt8(v & 0xFF))
        }
    }
    return d
}

let conn = NWConnection(host: NWEndpoint.Host(host), port: port, using: params)
let timer = DispatchSource.makeTimerSource(queue: .main)
var eofTicks = 0

func startSending() {
    timer.schedule(deadline: .now(), repeating: 1.0 / fps)
    timer.setEventHandler {
        lock.lock(); let frame = latest, have = haveFrame, done = eof; lock.unlock()
        if have { conn.send(content: packet(frame), completion: .contentProcessed { _ in }) }
        if done { eofTicks += 1; if eofTicks > 6 { conn.cancel(); exit(0) } }   // flush the final frame, then leave
    }
    timer.resume()
}

conn.stateUpdateHandler = { state in
    switch state {
    case .ready:
        print("READY")
        reader.start()
        startSending()
    case .failed(let e):
        fputs("ERROR connection failed: \(e)\n", stderr); exit(2)
    case .waiting(let e):
        fputs("waiting: \(e)\n", stderr)
    default: break
    }
}
conn.start(queue: .main)
DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
    var ok = false
    if case .ready = conn.state { ok = true }
    if !ok { fputs("ERROR DTLS handshake timed out (is the entertainment area started, and the key correct?)\n", stderr); exit(3) }
}
dispatchMain()
