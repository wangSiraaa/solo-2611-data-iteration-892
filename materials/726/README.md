只提供真实输入、独立手写格式样例及只读客户端，不提供页目录或引用后端。
python3 citation_client.py example_usable.jsonl example_needs_attention.jsonl
空JSONL文件表示没有记录；非空文件一行一个JSON对象，UTF-8，不是一个大JSON数组。每行format=citation-product/1，必须含catalog_revision、citation_id、text、kind、status、source、pages、reason。source保留原PINNED的卷/成员或RANGE的卷/两端；pages中每项含volume_id/page_id/section_id/label/active。usable原因null；needs_attention原因给稳定可解释字符串，可加详细字段。
可用RANGE的pages按当前卷内顺序。待处理RANGE不猜测或交换端点，pages列出两个已知端页及状态，source保留原顺序；PINNED即使成员停用也列出完整固定集合及状态，不删。PINNED集合没有页序语义，客户端按文件列出即可。

手算：初始C_PIN=[V1/p2,V1/p3]；C_RANGE=[V1/p2,V1/p3,V1/p4]。插px后RANGE多px，PINNED不变。把p4移到p2之前后，RANGE待处理而PINNED不变；停用p3后PINNED也待处理且仍含p3(inactive)。V2/p1和V1/p1是不同页。最后跨卷移动请求拒绝，不改变任何页。操作材料的JSON字段给业务请求，内部API可自定。
