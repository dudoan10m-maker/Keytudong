# TXAl payment-proof backend

## Luồng
1. Tool gọi POST /create-order -> server tạo orderId + số tiền + mã MUKEY-XXXX.
2. User chuyển khoản theo QR.
3. Tool OCR bill và gửi POST /payment-proof chỉ với orderId/device/proofText.
4. Server lấy giá + nội dung từ DB, không tin giá/nội dung do trình duyệt gửi.
5. Nếu số tiền + nội dung khớp, server tạo key và lưu vào inbox.
6. Tool poll GET /inbox?device=... và tự nhận key.

## PostgreSQL
Đặt DATABASE_URL của PostgreSQL vào Render. Bảng được tạo tự động khi app khởi động.

## Quan trọng về xác thực bill
OCR từ ảnh chỉ chứng minh "ảnh có chữ/số khớp", không chứng minh tiền thực sự vào tài khoản.
Muốn chống bill giả hoàn toàn, đặt BANK_VERIFY_URL trỏ tới API giao dịch ngân hàng/đơn vị thanh toán của bạn.
API đó nhận:
{"amount": 33000, "content": "MUKEY-XXXXXX", "orderId": "..."}
và trả:
{"valid": true}
