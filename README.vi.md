# Bộ crawler tài liệu PubMed + tự động bổ sung full text

Bộ script Python này biến kế hoạch từ khóa trong file YAML thành truy vấn
PubMed. Nó tải metadata của từng bài, lấy full text (XML và/hoặc PDF) từ các
nguồn hợp pháp, và ghi sổ (ledger) bài nào đã có, bài nào còn thiếu, và vì sao.

Bộ script được viết cho dự án khung tri thức về **thoái hóa hoàng điểm tuổi già
(AMD)**, và file config mẫu chính là dự án đó. Bạn dùng được cho bất kỳ bệnh hay
chủ đề nào: chỉ cần sửa YAML, không phải sửa code.

> 🇬🇧 English: [README.md](README.md)

---

## Nó làm gì

```
config.yaml ──► pubmed_crawler_v6_1.py ──► tìm trên PubMed (từ khóa + MeSH, theo từng năm)
                         │                    metadata của mọi bài khớp
                         │                    full text từ PMC / Unpaywall
                         ▼
                 output/_state/papers.csv  (ledger: mỗi bài một dòng)
                         │
                         ▼
               fill_fulltext.py  ──► lấp chỗ trống: PMC Cloud, thêm bản Unpaywall,
                         │             TDM API của nhà xuất bản, repository
                         ▼
          bài script không lấy được ──► --open (trình duyệt của bạn) ──► _inbox/ ──► tự xếp vào đúng chỗ
```

- **Tìm kiếm có cấu trúc.** Mỗi *branch* là một câu hỏi nghiên cứu. Mỗi *leaf*
  là một nhóm từ khóa bên trong branch đó. Mỗi truy vấn có dạng
  `anchor của bệnh AND (từ khóa của leaf OR MeSH của leaf)` và chạy theo từng năm,
  nên năm nhiều bài cũng không bị cắt.
- **Mỗi bài chỉ lưu một bản.** Bài được 5 leaf tìm thấy vẫn lưu một lần và gắn 5 tag.
- **Trạng thái rõ ràng.** Bài nào cũng có trạng thái và bước tiếp theo
  (`done`, `needs_browser`, `needs_tmu`, `retry_auto` ...). Không bài nào bị bỏ âm thầm.
- **Chạy lại được.** Bài đã xong sẽ được bỏ qua. File ghi dở sau khi crash được dọn,
  và ledger trước đó được giữ lại để hoàn tác.
- **File Excel tổng quan** `_index/eye_disease_papers.xlsx`: các sheet đã lưu / chưa
  lưu / cần quyền truy cập của trường, độ phủ theo branch và leaf, bài trùng, log chạy.

## Các file

| File | Dùng để |
|---|---|
| `pubmed_crawler_v6_1.py` | Crawler chính: tìm kiếm, metadata, lượt lấy full text đầu tiên |
| `fill_fulltext.py` | Đi lại ledger, thử thêm các nguồn cho XML/PDF còn thiếu |
| `organize_inbox.py` | Đổi tên PDF bạn tải tay rồi chép vào `_inbox/`; `--audit` so ledger với ổ đĩa |
| `check_inbox.py` | Chạy thử: mỗi file trong `_inbox/` sẽ được gán cho bài nào |
| `check_elsevier_pdf.py` | Kiểm tra Elsevier API trả về gì cho một DOI từ mạng hiện tại |
| `config.example.yaml` | Ví dụ mẫu (AMD, 1 branch, 2 leaf, năm 2013) |

`fill_fulltext.py` import crawler, nên hãy để các script chung một thư mục.

---

## ⚠️ Đọc trước khi dùng

- **Chỉ dùng nguồn hợp pháp:** PubMed/PMC, Europe PMC, bộ dữ liệu mở PMC trên AWS,
  Unpaywall, Crossref và API khai thác văn bản (TDM) chính thức của nhà xuất bản.
  Script không dùng thư viện lậu và không vượt paywall.
- **Quyền truy cập là của trường bạn, không phải của script.** Elsevier/Wiley TDM
  và route *publisher site* chỉ chạy trên mạng mà trường bạn có bản quyền (trong
  trường hoặc qua VPN). Hãy hỏi quy định của thư viện về tải tự động trước. Nhiều
  nhà xuất bản cấm tải hàng loạt từ website, và gửi quá nhiều request có thể khiến
  **IP của cả trường bị chặn**. Nếu không chắc, chạy `fill_fulltext.py --no-publisher`.
- **Không chia sẻ các bài báo.** `output/` chứa PDF có bản quyền. Đừng đưa nó lên
  git (`.gitignore` đã chặn sẵn) hay lên ổ đĩa công khai.
- **Đã có giới hạn tốc độ:** NCBI 3 req/s khi không có key, 10 req/s khi có key.
  Website nhà xuất bản 1 req/s. Đừng tăng lên.

---

## Cài đặt

Cần Python 3.9 trở lên.

```bash
git clone <repo này>
cd <repo này>
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Cài key (dùng biến môi trường, không ghi vào YAML)

| Biến | Có cần không? | Lấy ở đâu |
|---|---|---|
| `NCBI_API_KEY` | Nên có (3 → 10 req/s) | Tài khoản NCBI → Account settings → API Key Management |
| `ELSEVIER_API_KEY` | Tùy chọn | dev.elsevier.com (chỉ chạy từ mạng có bản quyền) |
| `ELSEVIER_INSTTOKEN` | Tùy chọn | Chỉ khi thư viện trường cấp cho bạn |
| `WILEY_TDM_TOKEN` | Tùy chọn | Wiley TDM click-through token (qua Crossref / ORCID) |

```bash
# Windows (rồi mở terminal MỚI)
setx NCBI_API_KEY "key-cua-ban"
# macOS / Linux (thêm vào ~/.bashrc hoặc ~/.zshrc)
export NCBI_API_KEY="key-cua-ban"
```

Script tự đọc các biến này. Script không bao giờ in key ra, và key không bao giờ
được ghi vào log hay file Excel.

---

## Bắt đầu nhanh

```bash
cp config.example.yaml config.yaml      # rồi điền email vào contact.email
python pubmed_crawler_v6_1.py config.yaml --check      # 1. in truy vấn, không dùng mạng
python pubmed_crawler_v6_1.py config.yaml --test-api   # 2. các API có trả lời không?
python pubmed_crawler_v6_1.py config.yaml              # 3. chạy thật
python fill_fulltext.py config.yaml --limit 20         # 4. lấp chỗ trống, thử 20 bài trước
python fill_fulltext.py config.yaml                    # 5. lấp toàn bộ
```

**Mẹo:** dán truy vấn mà `--check` in ra vào
[pubmed.ncbi.nlm.nih.gov](https://pubmed.ncbi.nlm.nih.gov). Số kết quả ở đó phải
khớp với số trong báo cáo chạy.

Config mẫu có sẵn "đáp án": năm 2013 có bài Ferris et al. 2013, *Clinical
classification of age-related macular degeneration* (PMID 23332590).
`--test-api` kiểm tra xem truy vấn có tìm ra bài này không.

---

## Vòng làm tay (bài script không lấy được)

Có bài miễn phí nhưng website đòi xác minh người thật ("are you a robot?"). Có bài
cần quyền truy cập của trường. Với những bài đó:

```bash
python fill_fulltext.py config.yaml --open 10      # mở 10 bài tiếp theo trên trình duyệt CỦA BẠN
# lưu từng PDF vào output/_inbox/  (đặt tên gì cũng được)
python check_inbox.py output                       # tùy chọn: xem file nào sẽ khớp bài nào
python fill_fulltext.py config.yaml --inbox-only   # xếp file vào đúng thư mục
```

PDF bạn đã có sẵn ở chỗ khác (Downloads, thư viện EndNote):

```bash
python organize_inbox.py output --from "C:\Users\you\Downloads"           # chỉ in kế hoạch
python organize_inbox.py output --from "C:\Users\you\Downloads" --apply   # chép vào _inbox
python fill_fulltext.py config.yaml --inbox-only
```

File được khớp theo thứ tự: DOI/PMID trong tên file, rồi DOI trong metadata của PDF,
rồi tiêu đề in ở mấy trang đầu. **PDF chỉ có 1 trang sẽ bị giữ lại**, vì Elsevier chỉ
gửi trang đầu khi mạng của bạn không có quyền. Nếu bài đó thật sự chỉ có 1 trang,
thêm `--allow-short-pdf`.

`python organize_inbox.py output --audit` so ledger với ổ đĩa và ghi ra
`structure_audit.csv`. Lệnh này không sửa gì cả.

---

## Cấu trúc output

```
output/
├── papers/<năm>/<PMCID_PMID hoặc noPMC_PMID>/
│       metadata.json   fulltext.xml   fulltext.pdf
├── _state/
│       papers.csv            ledger, mỗi bài một dòng
│       paper_branches.csv    mỗi dòng = bài × branch × leaf × cách gắn tag
│       queries.csv           mọi truy vấn đã chạy
│       runs.csv              mỗi lần chạy một dòng
│       fulltext_fill_log.csv mọi route đã thử, kèm phản hồi HTTP
│       run_report_latest.txt
│       previous/             ledger trước lần chạy gần nhất (để hoàn tác)
│       history/              báo cáo theo ngày
├── _index/
│       eye_disease_papers.xlsx
│       branches/<branch>/<leaf>.csv
│       duplicates.csv
├── _inbox/                   bỏ file tải tay vào đây
└── raw/                      XML gốc từ PubMed theo từng batch
```

### Cột `action` nghĩa là gì

| action | nghĩa | bạn làm gì |
|---|---|---|
| `done` | đã có full text | không cần làm gì |
| `needs_browser` | miễn phí nhưng web chặn script | `--open`, lưu vào `_inbox/` |
| `needs_tmu` | không mở; cần quyền truy cập của trường | ở trường/VPN: `--open`, hoặc nhờ thư viện |
| `needs_request` | có trên PMC nhưng bị đóng | yêu cầu qua thư viện |
| `retry_auto` | server không trả lời | chạy lại |
| `retry_later` | PMC đang embargo | chờ; sau 90 ngày sẽ được kiểm tra lại |
| `blocked_network` | không kết nối được tới host | kiểm tra firewall / VPN |
| `needs_code_fix` | không đọc được metadata | mở issue |

(`needs_tmu` là tên từ dự án gốc ở Đại học Y Đài Bắc (TMU). Hiểu là "cần quyền
truy cập của trường bạn".)

---

## Dùng cho chủ đề của bạn

Chỉ cần sửa YAML:

1. `disease.anchor`: mọi cách các bài báo gọi tên bệnh (có/không gạch nối, viết tắt).
2. `disease.mesh`: các MeSH descriptor của bệnh (tra ở meshb.nlm.nih.gov).
3. `branches`: mỗi câu hỏi nghiên cứu một branch. Bên trong là các `leaves`, mỗi
   leaf có `terms` (từ khóa tìm trong tiêu đề/tóm tắt) và `mesh` (tùy chọn).
4. `date_range`: giữ `split_by_year: true`. PubMed chỉ cho lật trang tối đa 10.000
   kết quả mỗi truy vấn.
5. Chạy `--check` và dán truy vấn vào PubMed trước khi chạy thật.

Sau đó đọc báo cáo chạy. Cột `only_here` trong `leaf_coverage` cho biết mỗi leaf
thêm được bao nhiêu bài. Nếu `mesh_only` trong `branch_coverage` lớn, thì hoặc bộ
từ khóa đang thiếu, hoặc một MeSH quá rộng.

Lưu ý: bài kiểm tra "đáp án" của `--test-api` đang ghi cứng bài AMD
(`BECKMAN_PMID` ở đầu file crawler). Nếu dùng chủ đề khác, đổi nó thành một bài
bạn chắc chắn phải tìm ra.

---

## Xử lý sự cố

| Hiện tượng | Nguyên nhân thường gặp |
|---|---|
| `reach NCBI` fail | firewall, proxy hoặc VPN, không phải lỗi code |
| Cảnh báo `CAPPED` | một truy vấn có quá nhiều kết quả; chia năm nhỏ hơn hoặc thu hẹp từ khóa |
| Cảnh báo `PUBMED IGNORED` | PubMed âm thầm bỏ một cụm từ trong ngoặc kép; viết lại cụm đó |
| PDF Elsevier chỉ có 1 trang | mạng của bạn không có quyền; kiểm tra bằng `check_elsevier_pdf.py <config> <doi>` |
| "is still open in another program" | đóng file Excel; script sẽ chờ, hoặc chạy lại thì nó chạy tiếp |
| PDF báo UNREADABLE | `pip install "pypdf[crypto]"` |

## Giới hạn

- Chỉ tìm trên PubMed (trường `source.database` chỉ là nhãn).
- Cách gắn tag theo từ khóa chỉ là tín hiệu tương đối: PubMed tự xử lý biến thể từ (stemming), còn bộ gắn tag thì không.
- Không bao giờ tự động lấy được 100% full text. Sẽ luôn có một phần bài cần thư viện hỗ trợ.

## Giấy phép & trích dẫn

MIT (xem `LICENSE`). Nếu bộ script giúp ích cho bạn, một lời nhắc tên hoặc một ngôi
sao trên GitHub là đủ vui rồi. Hoan nghênh issue và pull request.
