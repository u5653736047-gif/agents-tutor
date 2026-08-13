// assistant-ui 接入(T14):附件 AttachmentAdapter——把既有附件链路
// (chat-input.tsx D7-T2 的「发送时上传」语义)映射到 assistant-ui 的
// 附件适配器契约:
//   - add:登记为 requires-action/composer-send(发送时才上传——与旧
//     输入区同一语义,不产生「已上传但未发送」的悬置孤儿文件);
//   - send:逐文件经 uploadFile 上传,回执(file_id/name/content_type/
//     size)打包为 data-attachment part;runtime-provider 的 onNew 据此
//     还原契约 Attachment[] 提交流式主通道;
//   - remove:纯前端移除(孤儿文件回收是后端职责,D7-T2 注释);
//   - 类型白名单与旧输入区/后端一致(.pdf/.png/.jpg/.jpeg/.txt);
//   - 数量上限(MAX_ATTACHMENTS=3)由 composer-native 在选择侧截断。
//
// 上传函数经构造器注入(默认 api-client.uploadFile),单测可替换。

import type {
  AttachmentAdapter,
  CompleteAttachment,
  PendingAttachment,
} from "@assistant-ui/react";

import { uploadFile } from "@/lib/api-client";
import type { components } from "@/contracts/api.generated";

type Attachment = components["schemas"]["Attachment"];

/** data part 名:onNew 按此名从附件 content 里还原上传回执 */
export const ATTACHMENT_DATA_PART = "attachment-ref";

export type UploadFn = (file: File) => Promise<{
  content_type: string | null;
  file_id: string;
  name: string;
  size: number;
}>;

/** 从 CompleteAttachment 的 content 还原契约 Attachment(宽容读取) */
export function attachmentFromPart(
  attachment: CompleteAttachment,
): Attachment | null {
  const part = attachment.content.find(
    (candidate) => candidate.type === "data" && candidate.name === ATTACHMENT_DATA_PART,
  );
  const data =
    part && part.type === "data"
      ? (part.data as Partial<Attachment> | undefined)
      : undefined;
  if (
    !data ||
    typeof data.file_id !== "string" ||
    typeof data.name !== "string" ||
    typeof data.size !== "number"
  ) {
    return null;
  }
  // content_type 契约可空(string | null),原样透传
  return {
    content_type: typeof data.content_type === "string" ? data.content_type : null,
    file_id: data.file_id,
    name: data.name,
    size: data.size,
  };
}

export function createAttachmentAdapter(upload: UploadFn = uploadFile): AttachmentAdapter {
  return {
    accept: ".pdf,.png,.jpg,.jpeg,.txt",
    async add({ file }): Promise<PendingAttachment> {
      return {
        contentType: file.type,
        file,
        id: `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        name: file.name,
        // 发送时上传(与旧输入区同一语义)
        status: { type: "requires-action", reason: "composer-send" },
        type: file.type.startsWith("image/") ? "image" : "file",
      };
    },
    async remove() {
      // 纯前端移除;孤儿文件回收是后端职责(D7-T2 注释)
    },
    async send(attachment): Promise<CompleteAttachment> {
      const receipt = await upload(attachment.file);
      return {
        ...attachment,
        status: { type: "complete" },
        content: [
          {
            type: "data",
            name: ATTACHMENT_DATA_PART,
            data: {
              content_type: receipt.content_type,
              file_id: receipt.file_id,
              name: receipt.name,
              size: receipt.size,
            },
          },
        ],
      };
    },
  };
}
