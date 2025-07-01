import logging
import os
import re
import time
import threading
import requests
import datetime
from queue import Queue

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MAX_INPUT = 2000
CHUNK_SIZE = 400
MAX_RETRIES = 3




class DeepseekProcessor:
    def __init__(self):
        self.api_key = "sk-a59555fddb5b4f7399c58b61d0b00a96"
        self.request_queue = Queue()
        self.annotation_rules = {
            "role": "system",
            "content": """你是一个文字注解专家，能准确分词和排歧，请严格按下列规则处理输入的文本：
            一、分词规则：
            1. 词内一般不得有混合字符（汉字，字母，符号，数字），各类型需拆分开，但外文中的特定词汇连接符除外，例如“don't”视为一个整词，不能拆分。
            2. 外文一般按空格分词，但词组例外。  例： "have to"
            3.如果是中文、日文等，尽量拆分为较小的词组。
            4. 无论哪国字符，标点，换行，都原样反馈，不得遗漏。,禁止自己添加换行'\n'。
            二、注解规则：
            （一）原文是某国文字
            1、有词义的注解格式：原文[中文注解词+词性缩写]。 (例如：国[国家N])
            注意：词性缩写只能用下列缩写，若为其他词性，须选择与上表相近的标注。
            N=普通名词,F=方位,S=处所,T=时间,V=动词,A=形容,D=副词,M=数量,Q=量词,R=代词,P=介词,C=连词,U=助词、助动词,X=虚词和其他无义的不用翻译的词,NR=人名,NS=地名,NT=机构,NW=作品,NZ=其他专有名
            注解要求（关键）：1、注解词必须为中文，尽量选较常见的、简短的词，2、必须排歧：如果注解词加上词性标注后仍有歧义，须更换注解词，（例如：酒店N就有歧义，须换为饭店N或宾馆N),
            确实不便更换的，须确保当前义比其他歧义更常用。注意：单字词的歧义极多，因此尽量不用单字作注解词。例如：包V，有包装V、包围V、担保V 三个歧义。3、注解必须是本义，不能注解其性质类型，例如：定冠词、序数词、某某术语都是错误的。
            
            2、无词义的注解格式：原文[空义+词性缩写]
            指无需翻译的词（例如：the[空义X] ， 个[空义Q]）
            （二）原文是标点、阿拉伯数字、空义的单个的字母、不明含义或不便翻译的字母串
            注解格式：原文[原文 spec]
            注：原文是半角的须换成全角。空格无需变换。
            三、自检
            检查[]里的内容（里含“spec”或“空义”的除外），前面是否为中文，后面是否为上述规定的词性缩写字母，如果不是，须纠正。再检查是否有比当前义更常见的且词性相同的歧义，如果有，须更换注解词。
            四、综合示例：
            “包里的东西包你满意。”→“包[包包N]里[里面F]的[空义U]东西[物品N]包[保证V]你[你R]满意[满意V]。[。spec]"
            """

        }
        threading.Thread(target=self._process_queue, daemon=True).start()

    def _save_annotation(self, text, result):
        """保存注解到文件"""
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ds_annotation_{timestamp}.txt"
        with open(os.path.join(log_dir, filename), "w", encoding="utf-8") as f:
            f.write(f"=== 原始文本 ===\n{text}\n")
            f.write(f"=== 注解结果 ===\n{result}\n\n")

    def _split_text(self,text) -> list[str]:
        chunks, buf, buf_len = [], "", 0

        for idx,para in enumerate(text.split("\n")):
            if idx==len(text.split("\n")) and para=='':
                continue
            block = para + "\n"  # 带段落分隔符
            # 整段不超阈值，直接加
            if buf_len + len(block) <= CHUNK_SIZE:
                buf += block
                buf_len += len(block)
            else:
                # 段落本身超阈值，降级到句子级拆分
                if len(block) > CHUNK_SIZE:
                    # 先 flush 当前 buf
                    if buf:
                        chunks.append(buf)
                        buf = ""
                        buf_len = 0
                    for sent in re.split(r'(?<=[.!?。！？])', para):
                        sent = sent.strip()
                        if not sent: continue
                        sent += ""  # 不加额外分隔
                        if buf_len + len(sent) > CHUNK_SIZE:
                            if buf: chunks.append(buf)
                            buf, buf_len = sent, len(sent)
                        else:
                            buf += sent
                            buf_len += len(sent)
                    # 加回段落分隔
                    sep = "\n"
                    if buf_len + 1 <= CHUNK_SIZE:
                        buf += sep
                        buf_len += 1
                    else:
                        chunks.append(buf)
                        buf, buf_len = sep, 1
                else:
                    # 新块开始
                    if buf: chunks.append(buf)
                    buf, buf_len = block, len(block)

        if buf: chunks.append(buf)
        return chunks

    def _process_queue(self):
        """队列处理核心逻辑"""
        while True:
            callback, text = self.request_queue.get()
            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = requests.post(
                        DEEPSEEK_URL,
                        headers={"Authorization": f"Bearer {self.api_key}","Content-Type": "application/json"},
                        json={
                            "messages": [
                                self.annotation_rules,
                                {"role": "user", "content": text}
                            ],
                            "model": "deepseek-chat",
                            "temperature": 0.01
                        },
                        timeout=60
                    )
                    annotated = response.json()['choices'][0]['message']['content']
                    self._save_annotation(text, annotated)  # 保存日志
                    tokens = self._parse_annotation(annotated)
                    callback(tokens)
                    break
                except Exception as e:
                    logging.error(f"Error processing text: {e}")
                    if attempt >= MAX_RETRIES:
                        break
                    time.sleep(1)
            self.request_queue.task_done()

    def _parse_annotation(self, annotated_text: str) -> list[dict]:
        """
        解析注解文本：
        - 匹配形如 word~annotation 的标注对
        - 保留所有原始的换行符，作为单独的 token，annotation 和 pos 置空
        """
        tokens = []
        # 用于切分：把所有 word~ann 保留下来，其他当“非标注”文本
        # pattern = re.compile(r'(\S+?)\[(\S+?)([A-Za-z]+)\]')
        # pattern = re.compile(r'(\S*)\[\s*(\S*)?([A-Za-z]+)\]')
        # annotated_text=annotated_text.strip()
        pattern = re.compile(r'(\S*)\[\s*(\S*?)([A-Za-z]+)\]')

        results = pattern.findall(annotated_text)



        # results = [
        #     {"word": m.group("word"), "annotation": m.group("annotation"), "pos": m.group("pos")}
        #     for m in pattern.finditer(annotated_text)
        # ]
        # for r in results:
        #     tokens.append({
        #                 'word': r['word'],
        #                 'annotation': r['annotation'],
        #                 'pos': r['pos']
        #             })
        #     logging.info(f"原词: {r['word']}, 含义: {r['annotation']}, 词性: {r['pos']}")
        tokens = []
        # 先按换行符切分
        pattern = re.compile(
            r'(?P<word>[^\[\]]+?)\['  # 原词，尽可能少地匹配 `[` 前的内容
            r'(?P<annotation>[^\[\]]*?)'  # 含义，允许字母、数字等，只排除中括号
            r'(?P<pos>[A-Za-z]+)'  # 词性：至少一个字母
            r'\]'  # 右中括号
        )
        segments = re.split(r'(\n)', annotated_text)  # 用捕获组保留\n本身

        for seg in segments:
            if seg == '\n':
                tokens.append({'word': '\n', 'annotation': 'none', 'pos': 'none'})
            else:
                for m in pattern.finditer(seg):
                    if m.group("word")=='':
                        continue
                    annotation_raw=m.group("annotation")
                    if annotation_raw.strip() == "":  # 纯空格，保留
                        annotation = annotation_raw
                    else:
                        annotation = annotation_raw.rstrip()

                    r = {
                        'word': m.group("word"),
                        'annotation': annotation,
                        'pos': m.group("pos")
                    }
                    tokens.append(r)
                    logging.info(f"原词: {r['word']}, 含义: {r['annotation']}, 词性: {r['pos']}")


        # for word, meaning, pos in results:
        #     tokens.append({
        #                 'word': word,
        #                 'annotation': meaning,
        #                 'pos': pos
        #             })
        #     logging.info(f"原词: {word}, 含义: {meaning}, 词性: {pos}")
        # parts = re.split(r'(\S+?~\S+)', annotated_text)
        #
        # for part in parts:
        #     # 如果是标注段
        #     if re.fullmatch(r'\S+?~\S+', part):
        #         word, ann = part.split('~', 1)
        #         pos_match = re.findall(r'[A-Za-z]', ann)
        #         tokens.append({
        #             'word': word,
        #             'annotation': ann,
        #             'pos': ''.join(pos_match) if pos_match else ''
        #         })
        #     else:
        #         # 处理非标注段，只关注换行
        #         # 先将 \r\n 统一成 \n
        #         seg = part.replace('\r\n', '\n')
        #         # 按“多重换行”或者“单换行”来切分
        #         for run in re.findall(r'\n{2,}|\n', seg):
        #             if len(run) >= 2:
        #                 # 两个或以上换行为段落空行
        #                 tokens.append({'word': '\n\n', 'annotation': '', 'pos': ''})
        #             else:
        #                 # 单个换行为行内换行
        #                 tokens.append({'word': '\n', 'annotation': '', 'pos': ''})
        return tokens
    def async_process(self, text, callback):
        """异步处理入口"""
        for chunk in self._split_text(text):
            self.request_queue.put((callback, chunk))
