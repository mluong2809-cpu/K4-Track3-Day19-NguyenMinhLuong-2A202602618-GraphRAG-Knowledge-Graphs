# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Nguyễn Minh Lương · **MSSV:** 2A202602618 · **Ngày:** 05-10-2026

Thí nghiệm dùng 18 điều luật, 20 bài tin, 176 chunk, top-k = 3, Gemini 3.5 Flash-Lite cho chat/judge và Gemini Embedding 001 cho vector. Graph cuối cùng có 207 node và 376 cạnh. Các số dưới đây chép từ `ket_qua_benchmark_kg.txt` của lần chạy `--judge`.

## 1. Chi phí

### Indexing (một lần)

```text
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176         0        0   0.00000    119.2
graph       196     34619     5513   0.02417    204.3
```

### Querying (trung bình mỗi câu)

```text
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.51   1.33      696       72   0.00039     7.07
graph       1.00   2.00     7725      142   0.00267     2.07
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | ---: | ---: | ---: |
| Indexing USD | 0.00000 | 0.02417 | Không xác định (mẫu số 0) |
| Indexing giây | 119.2 | 204.3 | 1,71× |
| Mỗi câu: USD | 0.00039 | 0.00267 | 6,85× |
| Mỗi câu: giây | 7.07 | 2.07 | 0,29× |
| Mỗi câu: in_tok | 696 | 7.725 | 11,10× |

Graph indexing gọi thêm 20 lần LLM để trích xuất tin, tăng 85,1 giây và 0,02417 USD theo bộ đo. Querying Graph dùng prompt dài hơn khoảng 11 lần vì thêm dữ kiện vụ–luật, nên tăng 0,00228 USD/câu. **Giới hạn đo:** API embedding không trả token usage; mã hiện ghi 176 lượt embedding nhưng `in_tok=0`, `USD=0` cho Flat indexing. Đây là chi phí *được bộ đo ghi nhận*, không phải hóa đơn đầy đủ; không suy ra điểm hòa vốn theo tiền từ các số 0 này. Thời gian Flat trung bình cao chủ yếu do riêng Q5 mất 33,52 giây, không đủ để kết luận Graph luôn nhanh hơn.

## 2. Từng câu hỏi

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao |
| --- | --- | ---: | ---: | --- | --- |
| Q1 | Một nguồn luật | 1,00 / 2 | 1,00 / 2 | Hòa | Chunk luật đã có đủ định nghĩa tiền chất; Graph thêm Điều 2 khoản 4. |
| Q2 | Một nguồn tin | 1,00 / 2 | 1,00 / 2 | Hòa | Cả hai nêu Trần Thanh Tuấn và Trần Minh Tâm. |
| Q3 | Nối tin–luật | 0,33 / 1 | 1,00 / 2 | Graph | Flat nêu 36 tháng nhưng thiếu Điều 251 và khung 02–07 năm. |
| Q4 | Nối tin–luật | 0,33 / 1 | 1,00 / 2 | Graph | Flat biết hành vi Hoàng Nato nhưng thiếu Điều 255 và mức cao nhất chung thân. |
| Q5 | Đa bước | 0,40 / 1 | 1,00 / 2 | Graph | Graph nối Cái Quang Huy → MDMA → Điều 250 khoản 4; Flat thiếu khoản và hình phạt. |
| Q6 | Tổng hợp | 0,00 / 1 | 1,00 / 2 | Graph | Flat chỉ kể ba đoạn chunk, không nêu đủ tên vụ được benchmark yêu cầu; Graph duyệt các Case liên quan MDMA. |

Hai câu chỉ cần một tài liệu hòa điểm. Bốn câu cần nối hoặc tổng hợp đều được Graph chấm đủ ý; kết quả dựa trên 6 câu của corpus này, chưa chứng minh tính tổng quát trên bộ dữ liệu khác.

**Đối chứng ontology gợi ý:** Với cùng model/top-k/dữ liệu và cùng truy hồi, file `ket_qua_benchmark_kg.hint.txt` cho Graph `recall` trung bình 0,89 và `judge` 2,00; ontology chính đạt 1,00 và 2,00. Khác biệt tập trung ở Q6 (`recall` 0,33 → 1,00): câu trả lời chính nêu đầy đủ Cái Quang Huy, Lê Minh Thành, Viện Pháp y; bản gợi ý mô tả các vụ nhưng không nêu đủ tên theo bộ `must_include`. Đây là cải thiện theo phép đo keyword, không phải tăng điểm judge.

## 3. Phân tích lỗi

### E3 — Cùng một người thành nhiều node

- **Hiện tượng:** Cái Quang Huy là một người trong vụ vận chuyển từ Đức nhưng graph có hai `Person` khác nhau.
- **Bằng chứng:**

```cypher
MATCH (p:Person) WHERE p.name CONTAINS 'Quang Huy'
RETURN p.name AS name, p.id AS id, p.doc_id AS doc_id ORDER BY doc_id;
```

```text
Cái Quang Huy | news-100260917203001265#person-cái quang huy | news-100260917203001265
Cái Quang Huy | news-100260918080821054#person-cái quang huy | news-100260918080821054
```

Tin của Lê Minh Thành (`news-100260918080821054`) có phần giới thiệu bài liên quan đến Cái Quang Huy ở cuối, và LLM đã trích nó thành vụ/người thứ hai. Q6 Graph gộp hai mô tả vụ bằng chữ “hoặc”, che giấu việc trùng node.

- **Nguyên nhân:** Ở bước dựng graph, khóa `Person` theo `doc_id` tránh gộp nhầm người trùng tên nhưng không hợp nhất cùng người qua tài liệu. Bước trích xuất đọc cả phần bài liên quan ở cuối tài liệu.
- **Đề xuất sửa:** Tách phần bài chính khỏi mục bài liên quan trước extraction; thêm node người chuẩn theo tên + tín hiệu bổ sung như tuổi/vụ, còn `PersonMention` theo tài liệu giữ xuất xứ. Chỉ hợp nhất khi đủ tín hiệu, không `MERGE` theo tên đơn độc.

### E6 — Quan hệ có trường rỗng

- **Hiện tượng:** Phần lớn cạnh `INVOLVED_IN` có `sentence` là chuỗi rỗng, vì nhiều tin đang ở giai đoạn điều tra/truy tố hoặc không nêu bản án.
- **Bằng chứng:**

```cypher
MATCH (:Person)-[r:INVOLVED_IN]->(:Case)
RETURN count(r) AS total,
       count(CASE WHEN r.sentence = '' THEN 1 END) AS blank_sentence;
```

```text
total = 49, blank_sentence = 39
```

Chẳng hạn cạnh của Cái Quang Huy có `charge = 'vận chuyển trái phép chất ma túy'` nhưng `sentence = ''`; bài tin chỉ nói tòa chuẩn bị xét xử, chưa có án.

- **Nguyên nhân:** LLM được phép để trống mức án khi nguồn không có, nhưng Cypher vẫn `SET r.sentence = p.sentence`, biến “chưa biết” thành property rỗng. Ontology gộp thông tin cáo buộc và bản án trên cùng cạnh.
- **Đề xuất sửa:** Chỉ ghi `sentence` khi có nguồn nêu án; dùng `NULL`/không có property cho chưa biết và lưu giai đoạn vụ. Nếu mở rộng ontology, tách sự kiện xét xử/tuyên án khỏi quan hệ người tham gia.

## 4. Kết luận

Với corpus này, GraphRAG đáng dùng cho câu nối tin với điều luật (Q3–Q5) và tổng hợp nhiều vụ theo MDMA (Q6): bốn câu đó đều tăng từ judge 1 lên 2, trong khi Q1–Q2 hòa. Đổi lại, indexing mất thêm 85,1 giây và chi phí chat được ghi nhận tăng 0,02417 USD; mỗi câu tốn thêm khoảng 0,00228 USD và 7.029 input token. Flat RAG đủ cho câu chỉ cần một nguồn nếu mục tiêu là giảm chi phí. Chất lượng graph còn bị giới hạn bởi trích xuất phần bài liên quan và việc người cùng thực thể bị tách theo nguồn; điểm judge 2 của Q6 không tự chứng minh graph không có dữ kiện nhiễu.

## 5. Tự kiểm

```text
$ .venv\Scripts\python.exe -m pytest tests/ -q
................................................                         [100%]
48 passed in 0.08s

$ .venv\Scripts\python.exe bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = gemini:gemini-3.5-flash-lite | embedding = gemini:gemini-embedding-001
[OK] KG-2 build_graph: 148 node / 294 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 23 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00251.
```

Ba ảnh thật từ Neo4j Browser: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`. Người chọn cho ảnh vụ riêng là **Cái Quang Huy**. `kg_count.png` dùng kết quả bảng, còn hai ảnh đường đi có cột **Results overview** của giao diện Browser hiện tại.

## Vấn đề đã xử lý

Model Gemini 2.5 Flash-Lite mặc định bị API trả 404; đã đổi mặc định sang Gemini 3.5 Flash-Lite và cập nhật đơn giá token. Gemini embedding miễn phí giới hạn 100 request/phút; đã thêm chờ theo thời gian API trả để `--judge` chạy hết 176 chunk.
