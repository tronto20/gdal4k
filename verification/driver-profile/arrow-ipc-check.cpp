#include <iostream>
#include <limits>
#include <memory>
#include <string>
#include <vector>
#include <arrow/api.h>
#include <arrow/io/memory.h>
#include <arrow/ipc/api.h>
#include <arrow/util/future.h>
#include <arrow/util/int_util_overflow.h>

// The upstream regression exercises both zero-copy and coalesced async generators.
class CopyReader : public arrow::io::BufferReader {
 public:
  using BufferReader::BufferReader;
  bool supports_zero_copy() const override { return false; }
};

arrow::Status check() {
  using arrow::internal::AddWithOverflow;
  using arrow::internal::MultiplyWithOverflow;
  if (AddWithOverflow<int64_t>({std::numeric_limits<int64_t>::max(), 1}).has_value() ||
      MultiplyWithOverflow<int64_t>({std::numeric_limits<int64_t>::max(), 2}).has_value() ||
      AddWithOverflow<int64_t>({1, 2, 3}) != 6 ||
      MultiplyWithOverflow<int64_t>({2, 3, 4}) != 24) {
    return arrow::Status::Invalid("IPC checked-arithmetic regression");
  }
  std::vector<std::shared_ptr<arrow::RecordBatch>> expected;
  auto schema = arrow::schema({arrow::field("value", arrow::utf8_view())});
  for (int batch = 0; batch < 4; ++batch) {
    arrow::StringViewBuilder builder;
    builder.SetBlockSize(512);
    for (int i = 0; i < 256; ++i) {
      if (i % 7 == 0) { ARROW_RETURN_NOT_OK(builder.AppendNull()); }
      else { ARROW_RETURN_NOT_OK(builder.Append(std::string(64 + i % 41, 'a' + batch))); }
    }
    ARROW_ASSIGN_OR_RAISE(auto array, builder.Finish());
    if (array->data()->buffers.size() <= 3) return arrow::Status::Invalid("No variadic buffers exercised");
    expected.push_back(arrow::RecordBatch::Make(schema, array->length(), {array}));
  }
  ARROW_ASSIGN_OR_RAISE(auto sink, arrow::io::BufferOutputStream::Create());
  ARROW_ASSIGN_OR_RAISE(auto writer, arrow::ipc::MakeFileWriter(sink, schema));
  for (const auto& batch : expected) ARROW_RETURN_NOT_OK(writer->WriteRecordBatch(*batch));
  ARROW_RETURN_NOT_OK(writer->Close());
  ARROW_ASSIGN_OR_RAISE(auto bytes, sink->Finish());
  for (int repeat = 0; repeat < 16; ++repeat) {
    for (bool coalesce : {false, true}) for (bool prebuffer : {false, true}) {
      std::shared_ptr<arrow::io::RandomAccessFile> input;
      if (coalesce) input = std::make_shared<CopyReader>(bytes);
      else input = std::make_shared<arrow::io::BufferReader>(bytes);
      arrow::AsyncGenerator<std::shared_ptr<arrow::RecordBatch>> generator;
      {
        auto opened = arrow::ipc::RecordBatchFileReader::OpenAsync(input);
        ARROW_ASSIGN_OR_RAISE(auto reader, opened.result());
        if (prebuffer) ARROW_RETURN_NOT_OK(reader->PreBufferMetadata({}));
        ARROW_ASSIGN_OR_RAISE(generator, reader->GetRecordBatchGenerator(coalesce));
      } // Generator must retain its reader while async I/O is in flight.
      std::vector<arrow::Future<std::shared_ptr<arrow::RecordBatch>>> futures;
      for (size_t i = 0; i < expected.size(); ++i) futures.push_back(generator());
      auto last = generator();
      ARROW_ASSIGN_OR_RAISE(auto end, last.result());
      if (end) return arrow::Status::Invalid("Unexpected extra IPC batch");
      for (size_t i = 0; i < expected.size(); ++i) {
        ARROW_ASSIGN_OR_RAISE(auto batch, futures[i].result());
        if (!batch || !batch->Equals(*expected[i])) return arrow::Status::Invalid("IPC variadic buffer mismatch");
        ARROW_RETURN_NOT_OK(batch->ValidateFull());
      }
    }
  }
  return arrow::Status::OK();
}
int main() {
  auto status = check();
  if (!status.ok()) { std::cerr << status << '\n'; return 1; }
  std::cout << "GDAL4K_ARROW_IPC_VARIADIC_PREFETCH_OK\n";
}
