"""Build the credential-free Apple Shortcuts templates using only plistlib.

Two shortcuts:
1. Hiking · 上传 GPX: Previews the GPX name, optionally renames it, and saves as route or record.
2. Hiking · 批量上传照片: Batch selects/shares photos, uploads them, and auto-associates to matching records.
"""
import json
from pathlib import Path
import plistlib
import urllib.request
from uuid import NAMESPACE_URL, uuid5

SERVER_PLACEHOLDER = 'https://hiking.example.com'


def token(name):
    return {'Value': {'Type': 'Variable', 'VariableName': name},
            'WFSerializationType': 'WFTextTokenAttachment'}


def text(*parts):
    if len(parts) == 1 and isinstance(parts[0], dict) and parts[0].get('WFSerializationType') == 'WFTextTokenString':
        return parts[0]
    value, attachments = '', {}
    for part in parts:
        if isinstance(part, dict):
            offset = len(value.encode('utf-16-le')) // 2
            attachments[f'{{{offset}, 1}}'] = part['Value']
            value += '\ufffc'
        else:
            value += str(part)
    return {'Value': {'string': value, 'attachmentsByRange': attachments},
            'WFSerializationType': 'WFTextTokenString'}


def dictionary(values):
    return {'Value': {'WFDictionaryFieldValueItems': [
        {'WFItemType': 0, 'WFKey': text(key), 'WFValue': text(value)}
        for key, value in values.items()]}, 'WFSerializationType': 'WFDictionaryFieldValue'}


def build_gpx():
    actions = []

    def action(identifier, **parameters):
        uid = str(uuid5(NAMESPACE_URL, f'hiking-gpx-shortcut-v1/{len(actions)}'))
        actions.append({'WFWorkflowActionIdentifier': 'is.workflow.actions.' + identifier,
                        'WFWorkflowActionParameters': {'UUID': uid, **parameters}})
        return {'Value': {'Type': 'ActionOutput', 'OutputUUID': uid, 'OutputName': identifier},
                'WFSerializationType': 'WFTextTokenAttachment'}

    def remember(name, value):
        action('setvariable', WFVariableName=name, WFInput=value)
        return token(name)

    def stop_if(value, condition, message, **parameters):
        group = str(uuid5(NAMESPACE_URL, f'gpx-condition/{len(actions)}'))
        action('conditional', GroupingIdentifier=group, WFControlFlowMode=0,
               WFInput={'Type': 'Variable', 'Variable': value}, WFCondition=condition, **parameters)
        action('alert', WFAlertActionTitle='Hiking导入已停止', WFAlertActionMessage=message,
               WFAlertActionCancelButtonShown=False)
        action('exit')
        action('conditional', GroupingIdentifier=group, WFControlFlowMode=2)

    server_output = action('gettext', WFTextActionText=SERVER_PLACEHOLDER)
    token_output = action('gettext', WFTextActionText='在系统设置中生成上传凭证')
    server = remember('服务器地址', server_output)
    credential = remember('上传凭证', token_output)
    action('comment', WFCommentActionText='分享一个 GPX 文件（或运行选择文件） → 选择保存为路线或记录 → 显示读取的名称并选择是否重命名 → 上传到Hiking。记录和关联路线使用同一新名称。地址末尾不要带 /。首次运行允许网络及通知权限。')

    shortcut_input = {'Value': {'Type': 'ExtensionInput'}, 'WFSerializationType': 'WFTextTokenAttachment'}
    input_count = action('count', Input=shortcut_input, WFCountType='Items')
    input_count_text = action('gettext', WFTextActionText=text(input_count))

    file_group = str(uuid5(NAMESPACE_URL, f'gpx-file-group/{len(actions)}'))
    action('conditional', GroupingIdentifier=file_group, WFControlFlowMode=0,
           WFInput={'Type': 'Variable', 'Variable': input_count_text}, WFCondition=4, WFConditionalActionString='0')
    picked = action('documentpicker.open', WFFileSelectMultiple=False)
    action('setvariable', WFVariableName='GPX文件', WFInput=picked)
    action('conditional', GroupingIdentifier=file_group, WFControlFlowMode=1)
    gv = action('getvariable', WFVariable=shortcut_input)
    action('setvariable', WFVariableName='GPX文件', WFInput=gv)
    action('conditional', GroupingIdentifier=file_group, WFControlFlowMode=2)

    gpx_file = token('GPX文件')
    file_count = action('count', Input=gpx_file, WFCountType='Items')
    file_count_text = action('gettext', WFTextActionText=text(file_count))
    stop_if(file_count_text, 4, '未选择任何 GPX 文件，已停止上传。', WFConditionalActionString='0')

    menu_group = str(uuid5(NAMESPACE_URL, f'gpx-kind-menu/{len(actions)}'))
    action('choosefrommenu', GroupingIdentifier=menu_group, WFControlFlowMode=0,
           WFMenuItems=['路线', '记录'], WFMenuPrompt='请选择上传类型（路线或记录）')

    action('choosefrommenu', GroupingIdentifier=menu_group, WFControlFlowMode=1, WFMenuItemTitle='路线')
    route_type = action('gettext', WFTextActionText='route')
    action('setvariable', WFVariableName='类型', WFInput=route_type)
    route_label = action('gettext', WFTextActionText='路线')
    action('setvariable', WFVariableName='类型名称', WFInput=route_label)

    action('choosefrommenu', GroupingIdentifier=menu_group, WFControlFlowMode=1, WFMenuItemTitle='记录')
    record_type = action('gettext', WFTextActionText='record')
    action('setvariable', WFVariableName='类型', WFInput=record_type)
    record_label = action('gettext', WFTextActionText='记录')
    action('setvariable', WFVariableName='类型名称', WFInput=record_label)

    action('choosefrommenu', GroupingIdentifier=menu_group, WFControlFlowMode=2)

    authorization = text('Bearer ', credential)
    headers = dictionary({'Authorization': authorization, 'X-Hiking-Kind': token('类型'), 'Accept': 'application/json'})
    preview = action('downloadurl', WFURL=text(server, '/api/shortcut/gpx/preview?kind=', token('类型')),
                     WFHTTPMethod='POST', WFHTTPBodyType='File',
                     WFRequestVariable=gpx_file, WFHTTPHeaders=headers)
    preview_dict = action('detect.dictionary', WFInput=preview)
    preview_error = action('getvalueforkey', WFInput=preview_dict, WFDictionaryKey='error', WFGetDictionaryValueType='Value')
    stop_if(preview_error, 100, text(preview_error))
    current_name = action('getvalueforkey', WFInput=preview_dict, WFDictionaryKey='name', WFGetDictionaryValueType='Value')
    stop_if(current_name, 101, '服务器没有返回 GPX 名称，请检查服务器地址与接口版本。')
    current_name = remember('当前名称', current_name)
    remember('名称参数', action('gettext', WFTextActionText=''))

    rename_group = str(uuid5(NAMESPACE_URL, f'gpx-rename-menu/{len(actions)}'))
    action('choosefrommenu', GroupingIdentifier=rename_group, WFControlFlowMode=0,
           WFMenuItems=['保留原名', '重命名'],
           WFMenuPrompt=text('当前读取的名称：「', current_name, '」\n是否重命名？保存为记录时，记录和关联路线会同步使用新名称。'))
    action('choosefrommenu', GroupingIdentifier=rename_group, WFControlFlowMode=1, WFMenuItemTitle='保留原名')
    action('choosefrommenu', GroupingIdentifier=rename_group, WFControlFlowMode=1, WFMenuItemTitle='重命名')
    new_name = action('ask', WFAskActionPrompt='请输入新名称（1～160 字符）', WFInputType='Text',
                      WFAskActionDefaultAnswer=text(current_name), WFAllowsMultilineText=False)
    stop_if(new_name, 101, '新名称不能为空，已停止上传。')
    encoded_name = action('urlencode', WFInput=text(new_name), WFEncodeMode='Encode')
    remember('名称参数', action('gettext', WFTextActionText=text('&name=', encoded_name)))
    action('choosefrommenu', GroupingIdentifier=rename_group, WFControlFlowMode=2)

    response = action('downloadurl', WFURL=text(server, '/api/shortcut/gpx?kind=', token('类型'), token('名称参数')),
                      WFHTTPMethod='POST', WFHTTPBodyType='File',
                      WFRequestVariable=gpx_file, WFHTTPHeaders=headers)
    response = remember('导入响应', response)
    resp_dict = action('detect.dictionary', WFInput=response)
    error = action('getvalueforkey', WFInput=resp_dict, WFDictionaryKey='error', WFGetDictionaryValueType='Value')
    stop_if(error, 100, text(error))
    item_id = action('getvalueforkey', WFInput=resp_dict, WFDictionaryKey='item_id', WFGetDictionaryValueType='Value')
    stop_if(item_id, 101, '服务器没有返回条目编号，请检查服务器地址与接口版本。')
    name = action('getvalueforkey', WFInput=resp_dict, WFDictionaryKey='name', WFGetDictionaryValueType='Value')

    action('showresult', Text=text('导入完成：已成功保存为', token('类型名称'), '「', name, '」（编号 ', item_id, '）。'))

    return {'WFWorkflowName': 'Hiking · 上传 GPX', 'WFWorkflowClientVersion': '3036.0.4',
            'WFWorkflowMinimumClientVersion': 900, 'WFWorkflowMinimumClientVersionString': '900',
            'WFWorkflowIcon': {'WFWorkflowIconStartColor': 12365313, 'WFWorkflowIconGlyphNumber': 59511},
            'WFWorkflowTypes': ['ActionExtension'],
            'WFWorkflowInputContentItemClasses': ['WFGenericFileContentItem'],
            'WFWorkflowOutputContentItemClasses': [], 'WFWorkflowHasShortcutInputVariables': True,
            'WFWorkflowImportQuestions': [
                {'ActionIndex': 0, 'Category': 'Parameter', 'ParameterKey': 'WFTextActionText',
                 'Text': 'Hiking服务器地址（末尾不带 /）', 'DefaultValue': SERVER_PLACEHOLDER},
                {'ActionIndex': 1, 'Category': 'Parameter', 'ParameterKey': 'WFTextActionText',
                 'Text': '粘贴Hiking系统设置中生成的上传凭证', 'DefaultValue': ''}],
            'WFWorkflowActions': actions}


def build_photos():
    actions = []

    def action(identifier, **parameters):
        uid = str(uuid5(NAMESPACE_URL, f'hiking-photos-shortcut-v1/{len(actions)}'))
        actions.append({'WFWorkflowActionIdentifier': 'is.workflow.actions.' + identifier,
                        'WFWorkflowActionParameters': {'UUID': uid, **parameters}})
        return {'Value': {'Type': 'ActionOutput', 'OutputUUID': uid, 'OutputName': identifier},
                'WFSerializationType': 'WFTextTokenAttachment'}

    def remember(name, value):
        action('setvariable', WFVariableName=name, WFInput=value)
        return token(name)

    def stop_if(value, condition, message, **parameters):
        group = str(uuid5(NAMESPACE_URL, f'photos-condition/{len(actions)}'))
        action('conditional', GroupingIdentifier=group, WFControlFlowMode=0,
               WFInput={'Type': 'Variable', 'Variable': value}, WFCondition=condition, **parameters)
        action('alert', WFAlertActionTitle='Hiking照片上传已停止', WFAlertActionMessage=message,
               WFAlertActionCancelButtonShown=False)
        action('exit')
        action('conditional', GroupingIdentifier=group, WFControlFlowMode=2)

    server_output = action('gettext', WFTextActionText=SERVER_PLACEHOLDER)
    token_output = action('gettext', WFTextActionText='在系统设置中生成上传凭证')
    server = remember('服务器地址', server_output)
    credential = remember('上传凭证', token_output)
    action('comment', WFCommentActionText='批量选择或分享照片 → 逐张上传并自动关联到时间匹配的徒步记录。地址末尾不要带 /。首次运行允许网络、照片及通知权限；运行中保持快捷指令在前台。')

    shortcut_input = {'Value': {'Type': 'ExtensionInput'}, 'WFSerializationType': 'WFTextTokenAttachment'}
    input_count = action('count', Input=shortcut_input, WFCountType='Items')
    input_count_text = action('gettext', WFTextActionText=text(input_count))

    photos_group = str(uuid5(NAMESPACE_URL, f'photos-branch/{len(actions)}'))
    action('conditional', GroupingIdentifier=photos_group, WFControlFlowMode=0,
           WFInput={'Type': 'Variable', 'Variable': input_count_text}, WFCondition=4, WFConditionalActionString='0')
    picked = action('selectphotos', WFSelectMultiple=True)
    action('setvariable', WFVariableName='待上传照片', WFInput=picked)
    action('conditional', GroupingIdentifier=photos_group, WFControlFlowMode=1)
    gv = action('getvariable', WFVariable=shortcut_input)
    action('setvariable', WFVariableName='待上传照片', WFInput=gv)
    action('conditional', GroupingIdentifier=photos_group, WFControlFlowMode=2)

    photos = token('待上传照片')
    total = remember('照片总数', action('count', Input=photos, WFCountType='Items'))
    total_text = action('gettext', WFTextActionText=text(total))
    stop_if(total_text, 4, '未选择任何照片，已停止上传。', WFConditionalActionString='0')

    authorization = text('Bearer ', credential)

    loop = str(uuid5(NAMESPACE_URL, 'hiking-photo-upload-loop'))
    action('repeat.each', WFInput=photos, GroupingIdentifier=loop, WFControlFlowMode=0)
    original = token('Repeat Item')
    taken_at = action('properties.images', WFInput=original, WFContentItemPropertyName='Date Taken')
    taken_at = action('format.date', WFDate=text(taken_at), WFDateFormatStyle='Custom',
                      WFDateFormat="yyyy-MM-dd'T'HH:mm:ss.SSSXXX", WFTimeFormatStyle='None')
    stop_if(taken_at, 101, '无法取得照片拍摄时间，已停止上传。此前成功上传的照片会保留。')
    taken_at = remember('拍摄时间', taken_at)
    resized = action('image.resize', WFImage=original, WFImageResizeKey='Longest Edge', WFImageResizeLength=720)
    jpeg = action('image.convert', WFInput=resized, WFImageFormat='JPEG',
                  WFImageCompressionQuality=0.85, WFImagePreserveMetadata=True)
    uploaded = action('downloadurl', WFURL=text(server, '/api/shortcut/photos'),
                      WFHTTPMethod='POST', WFHTTPBodyType='File', WFRequestVariable=jpeg,
                      WFHTTPHeaders=dictionary({'Authorization': authorization, 'X-Photo-Taken-At': taken_at,
                                                'Content-Type': 'image/jpeg', 'Accept': 'application/json'}))
    uploaded = remember('照片响应', uploaded)
    resp_dict = action('detect.dictionary', WFInput=uploaded)
    error = action('getvalueforkey', WFInput=resp_dict, WFDictionaryKey='error', WFGetDictionaryValueType='Value')
    stop_if(error, 100, text('第 ', token('Repeat Index'), ' 张上传失败：', error, '。此前已上传的照片会保留。'))
    photo_id = action('getvalueforkey', WFInput=resp_dict, WFDictionaryKey='id', WFGetDictionaryValueType='Value')
    stop_if(photo_id, 101, '上传响应无效，已停止。此前成功上传的照片会保留。')
    action('notification', WFNotificationActionTitle='Hiking · 照片上传进度',
           WFNotificationActionBody=text('已完成 ', token('Repeat Index'), ' / ', total), WFNotificationActionSound=False)
    action('repeat.each', GroupingIdentifier=loop, WFControlFlowMode=2)

    action('showresult', Text=text('照片上传完成：共处理 ', total, ' 张照片，已自动关联到对应徒步记录（未匹配或重复照片自动跳过）。'))

    return {'WFWorkflowName': 'Hiking · 批量上传照片', 'WFWorkflowClientVersion': '3036.0.4',
            'WFWorkflowMinimumClientVersion': 900, 'WFWorkflowMinimumClientVersionString': '900',
            'WFWorkflowIcon': {'WFWorkflowIconStartColor': 4282601983, 'WFWorkflowIconGlyphNumber': 59600},
            'WFWorkflowTypes': ['ActionExtension'],
            'WFWorkflowInputContentItemClasses': ['WFImageContentItem'],
            'WFWorkflowOutputContentItemClasses': [], 'WFWorkflowHasShortcutInputVariables': True,
            'WFWorkflowImportQuestions': [
                {'ActionIndex': 0, 'Category': 'Parameter', 'ParameterKey': 'WFTextActionText',
                 'Text': 'Hiking服务器地址（末尾不带 /）', 'DefaultValue': SERVER_PLACEHOLDER},
                {'ActionIndex': 1, 'Category': 'Parameter', 'ParameterKey': 'WFTextActionText',
                 'Text': '粘贴Hiking系统设置中生成的上传凭证', 'DefaultValue': ''}],
            'WFWorkflowActions': actions}


def build():
    return build_gpx()


def sign_remote(plist_bytes, name):
    payload = json.dumps({'shortcutName': name, 'shortcut': plist_bytes.decode('utf-8')}).encode('utf-8')
    req = urllib.request.Request(
        'https://hubsign.routinehub.services/sign',
        data=payload,
        headers={'Content-Type': 'application/json', 'User-Agent': 'cherri/0.0.1'}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status == 200:
            content = resp.read()
            if content.startswith(b'AEA1'):
                return content
    raise RuntimeError('HubSign failed to sign shortcut')


def main():
    root = Path(__file__).resolve().parent.parent / 'shortcuts'
    root.mkdir(exist_ok=True)

    items = [
        ('hiking-gpx-unsigned.shortcut', 'hiking-gpx.shortcut', build_gpx(), 'hiking-gpx'),
        ('hiking-photos-unsigned.shortcut', 'hiking-photos.shortcut', build_photos(), 'hiking-photos')
    ]

    for unsigned_name, signed_name, template, base in items:
        unsigned_path = root / unsigned_name
        signed_path = root / signed_name
        xml_bytes = plistlib.dumps(template, fmt=plistlib.FMT_XML, sort_keys=False)
        unsigned_path.write_bytes(xml_bytes)
        print(f'Wrote {unsigned_path}')

        try:
            signed_bytes = sign_remote(xml_bytes, base)
            signed_path.write_bytes(signed_bytes)
            print(f'Signed {signed_path} ({len(signed_bytes)} bytes)')
        except Exception as e:
            print(f'Remote signing for {base} skipped or failed: {e}')


if __name__ == '__main__':
    main()
