//
//  main.swift
//  OKCard
//
//  Created by Farsonic on 10/21/26.
//



import Foundation

// --- config -------------------------------------------------------------
let baseURL   = "https://okapp.ok-corporation.co.jp"
let appUser   = "okapp"            // static app-gate (HTTP Basic) username
let appPass   = "hs6sfeja8"        // static app-gate (HTTP Basic) password
let userAgent = "okstore v1.0.0) " // keep exactly as-is (matches the app)

func die(_ msg: String) -> Never {
    FileHandle.standardError.write(("error: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

// --- args ---------------------------------------------------------------
let args = CommandLine.arguments
guard args.count >= 2 else { die("usage: \(args[0]) YYYY-MM   (e.g. \(args[0]) 2026-09)") }
let ym = args[1]
guard ym.range(of: #"^\d{4}-\d{2}$"#, options: .regularExpression) != nil else {
    die("usage: \(args[0]) YYYY-MM   (e.g. \(args[0]) 2026-09)")
}

// --- credentials --------------------------------------------------------
// Asked on every run; re-asks while empty, exits on end of input (Ctrl-D).
// In a terminal the password is not echoed; Xcode's console has no terminal,
// so there it is read as a plain line.
func prompt(_ label: String, secret: Bool = false) -> String {
    while true {
        let line: String?
        if secret && isatty(STDIN_FILENO) != 0 {
            line = getpass(label).map { String(cString: $0) }
        } else {
            FileHandle.standardError.write(label.data(using: .utf8)!)
            line = readLine(strippingNewline: true)
        }
        guard let value = line else { die("no input") }
        if !value.isEmpty { return value }
    }
}

let loginId  = prompt("OKCard login (email): ")
let password = prompt("OKCard password: ", secret: true)

// --- synchronous HTTP helper -------------------------------------------
func request(_ urlString: String,
             method: String = "GET",
             headers: [String: String] = [:],
             body: Data? = nil) -> (status: Int, data: Data) {
    guard let url = URL(string: urlString) else { die("bad url: \(urlString)") }
    var req = URLRequest(url: url)
    req.httpMethod = method
    req.setValue(userAgent, forHTTPHeaderField: "User-Agent")
    for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }
    req.httpBody = body

    var result: (Int, Data) = (-1, Data())
    let sem = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: req) { data, resp, err in
        defer { sem.signal() }
        if let err = err { die("request to \(urlString) failed: \(err.localizedDescription)") }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? -1
        result = (code, data ?? Data())
    }.resume()
    sem.wait()
    return result
}

func asInt(_ v: Any?) -> Int {
    switch v {
    case let n as NSNumber: return n.intValue
    case let s as String:   return Int(Double(s) ?? 0)
    default:                return 0
    }
}

let grouper: NumberFormatter = {
    let f = NumberFormatter(); f.numberStyle = .decimal; f.groupingSeparator = ","; return f
}()
func yen(_ n: Int) -> String { "¥" + (grouper.string(from: NSNumber(value: n)) ?? "\(n)") }

// --- 1. login -----------------------------------------------------------
// Matches the .ps1: POST JSON + User-Agent only, no app-gate basic auth here.
let loginBody = try! JSONSerialization.data(
    withJSONObject: ["login_id": loginId, "password": password])
let (loginCode, loginData) = request(
    baseURL + "/auth/login",
    method: "POST",
    headers: ["Content-Type": "application/json"],
    body: loginBody)

func jsonObject(_ data: Data) -> [String: Any]? {
    (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
}

guard let login = jsonObject(loginData),
      let response = login["response"] as? [String: Any],
      let token = response["access_token"] as? String, !token.isEmpty else {
    let raw = String(data: loginData, encoding: .utf8) ?? "<non-text response>"
    die("login failed (HTTP \(loginCode)): \(raw)")
}

// --- 2. fetch full purchase history -------------------------------------
// last_access_date is a lower bound; an old date returns everything, then we
// filter to the requested month locally. App-gate basic auth goes here.
let basic = Data("\(appUser):\(appPass)".utf8).base64EncodedString()
let (histCode, histData) = request(
    baseURL + "/store/purchase_history?last_access_date=2000-01-01%2000%3A00%3A00",
    headers: [
        "Authorization": "Basic \(basic)",
        "Idtoken": token,
        "Content-Type": "application/json; charset=utf-8",
    ])

guard let hist = jsonObject(histData),
      (hist["result"] as? String) == "success",
      let purchases = hist["response"] as? [[String: Any]] else {
    let raw = String(data: histData, encoding: .utf8) ?? "<non-text response>"
    die("history fetch failed (HTTP \(histCode)): \(raw)")
}

// --- 3. render list + summary for the month -----------------------------
let month = purchases
    .filter { ($0["purchase_date"] as? String)?.hasPrefix(ym) == true }
    .sorted { ($0["purchase_date"] as? String ?? "") < ($1["purchase_date"] as? String ?? "") }

print("=== OKCard purchases \(ym) ===")
print("")

if month.isEmpty {
    print("No purchases found for \(ym).")
    exit(0)
}

var totalSpent = 0
var totalItems = 0
for p in month {
    let date  = p["purchase_date"] as? String ?? ""
    let store = p["store_name"]    as? String ?? ""
    let price = asInt(p["purchase_price"])
    totalSpent += price
    print("[\(date)]  \(store)   \(yen(price))")
    for it in (p["items"] as? [[String: Any]] ?? []) {
        let name  = it["item_name"]  as? String ?? ""
        let count = asInt(it["item_count"])
        totalItems += count
        print("    - \(name)  x\(count)   \(yen(asInt(it["price"])))")
    }
    print("")
}

print("--- Summary \(ym) ---")
print("Purchases:   \(month.count)")
print("Total items: \(totalItems)")
print("Total spent: \(yen(totalSpent))")


