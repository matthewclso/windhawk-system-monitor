// SPDX-License-Identifier: GPL-3.0-only
// Native Notification Center telemetry panel. Collector owns account/network
// work. The background MTA reads <=64 KiB of sanitized schema-v1 JSON; immutable
// snapshots cross to the shell UI thread under a mutex. All XAML construction,
// mutation and teardown happen on that object's owning thread. Weak references
// permit flyouts to be destroyed without extending their lifetime.
#include <winrt/Windows.Data.Json.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Foundation.Collections.h>
#include <winrt/Windows.UI.Xaml.Markup.h>
#include <winrt/Windows.UI.Xaml.Media.h>
#include <winrt/Windows.UI.Xaml.Controls.h>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <array>
#include <fstream>
#include <sstream>
#include <memory>

namespace ncm {
namespace xaml=winrt::Windows::UI::Xaml;
namespace controls=xaml::Controls;
struct Snapshot {
    std::array<std::wstring,8> values{L"Starting…",L"Starting…",L"Starting…",L"Starting…"};
    std::array<std::wstring,8> tips{};
    std::array<bool,8> visible{true,true,true,true,false,false,false,false};
};
std::mutex mutex;
std::condition_variable wake;
std::thread reader;
bool stopping=false;
std::shared_ptr<const Snapshot> snapshot=std::make_shared<Snapshot>();

std::wstring DataPath() {
    wchar_t buffer[32768];
    DWORD count=GetEnvironmentVariableW(L"LOCALAPPDATA",buffer,32768);
    if (!count || count>=32768) return {};
    return std::wstring(buffer)+L"\\NotificationCenterMetrics\\panel.json";
}
void Start() {
    stopping=false;
    reader=std::thread([] {
        winrt::init_apartment(winrt::apartment_type::multi_threaded);
        auto path=DataPath();
        while (true) {
            std::shared_ptr<Snapshot> next;
            try {
                // FILE_SHARE_DELETE permits the helper to atomically replace snapshots.
                HANDLE file=CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,
                                        nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
                if (file!=INVALID_HANDLE_VALUE) {
                    DWORD size=GetFileSize(file,nullptr),read=0;
                    std::string text;
                    if (size>0 && size<=65536) { text.resize(size);ReadFile(file,text.data(),size,&read,nullptr); }
                    CloseHandle(file);
                    if (read==size && !text.empty()) {
                        auto json=winrt::Windows::Data::Json::JsonObject::Parse(winrt::to_hstring(text));
                        auto rows=json.GetNamedArray(L"rows");
                        if (rows.Size()==8) {
                            next=std::make_shared<Snapshot>();
                            for (unsigned i=0;i<8;i++) {
                                auto row=rows.GetObjectAt(i);
                                next->values[i]=row.GetNamedString(L"value",L"Unavailable");
                                next->tips[i]=row.GetNamedString(L"tooltip",L"");
                                next->visible[i]=row.GetNamedBoolean(L"visible",i<4);
                            }
                            FILETIME now{};GetSystemTimeAsFileTime(&now);
                            ULARGE_INTEGER clock{now.dwLowDateTime,now.dwHighDateTime};
                            double age=clock.QuadPart/10000000.0-11644473600.0-json.GetNamedNumber(L"generatedAt",0);
                            if (age>10 || age < -60) {
                                for (unsigned i=0;i<4;i++) {next->values[i]=L"Unavailable";next->tips[i]=L"Metrics helper is not running.";}
                                for (unsigned i=4;i<8;i++) if (next->visible[i]) {
                                    next->values[i]+=L" · cached";next->tips[i]+=L"\nMetrics helper is not running.";
                                }
                            }
                        }
                    }
                } else {
                    next=std::make_shared<Snapshot>();
                    for (unsigned i=0;i<4;i++) {next->values[i]=L"Unavailable";next->tips[i]=L"Start the Notification Center Metrics helper.";}
                }
            } catch (...) { next.reset(); /* Reject incomplete snapshots as a unit. */ }
            if (!next) {
                next=std::make_shared<Snapshot>();
                for (unsigned i=0;i<4;i++) {next->values[i]=L"Unavailable";next->tips[i]=L"Metrics snapshot unavailable.";}
            }
            std::unique_lock lock(mutex);
            if (next) snapshot=next;
            if (wake.wait_for(lock,std::chrono::seconds(1),[]{return stopping;})) break;
        }
        winrt::uninit_apartment();
    });
}
void Stop() {
    {std::lock_guard lock(mutex);stopping=true;}
    wake.notify_all();
    if (reader.joinable()) reader.join();
}
struct Panel {
    winrt::weak_ref<controls::Grid> host;
    controls::StackPanel content{nullptr};
    controls::RowDefinition row{nullptr};
    std::array<controls::Grid,8> rows{};
    std::array<controls::TextBlock,8> texts{};
    std::vector<std::pair<winrt::weak_ref<xaml::FrameworkElement>,int>> originalRows;
    bool madeOriginalRow=false;
};
thread_local std::vector<Panel> panels;
thread_local xaml::DispatcherTimer timer{nullptr};
thread_local winrt::event_token tick{};
thread_local bool attaching=false;

void Update(Panel& panel) {
    if (!panel.content) return;
    std::shared_ptr<const Snapshot> data;
    {std::lock_guard lock(mutex);data=snapshot;}
    for (unsigned i=0;i<8;i++) {
        panel.rows[i].Visibility(data->visible[i]?xaml::Visibility::Visible:xaml::Visibility::Collapsed);
        if (panel.texts[i].Text()!=data->values[i]) panel.texts[i].Text(data->values[i]);
        controls::ToolTipService::SetToolTip(panel.rows[i],winrt::box_value(data->tips[i]));
    }
}
void Attach(Panel& panel) {
    auto host=panel.host.get();
    if (!host || panel.content || host.Children().Size()==0 || !host.IsLoaded()) return;
    attaching=true;
    try {
        panel.content=controls::StackPanel();panel.content.Name(L"NcmMetricsPanel");
        panel.content.Margin({16,12,16,10});panel.content.Spacing(4);
        static const wchar_t* labels[]={L"CPU",L"RAM",L"Disk C:",L"GPU",L"Codex",L"Resets",L"Claude",L"Resets"};
        for (unsigned i=0;i<8;i++) {
            auto row=controls::Grid();row.Name(winrt::hstring(L"NcmRow")+winrt::to_hstring(i));
            // Make empty row space hit-testable so the tooltip works across it.
            row.Background(xaml::Media::SolidColorBrush(winrt::Windows::UI::Color{0,0,0,0}));
            controls::ColumnDefinition labelColumn;labelColumn.Width({62,xaml::GridUnitType::Pixel});
            controls::ColumnDefinition valueColumn;valueColumn.Width({1,xaml::GridUnitType::Star});
            row.ColumnDefinitions().Append(labelColumn);row.ColumnDefinitions().Append(valueColumn);
            auto label=controls::TextBlock();label.Text(labels[i]);label.FontSize(12);label.VerticalAlignment(xaml::VerticalAlignment::Top);
            auto value=controls::TextBlock();value.FontSize(12);value.TextWrapping(xaml::TextWrapping::Wrap);
            value.TextAlignment(xaml::TextAlignment::Right);controls::Grid::SetColumn(value,1);
            if (i==4 || i==6) row.Margin({0,7,0,0});
            if (i==5 || i==7) {label.Opacity(0.7);value.Opacity(0.7);}
            row.Children().Append(label);row.Children().Append(value);panel.content.Children().Append(row);
            panel.rows[i]=row;panel.texts[i]=value;
        }
        // Prepend one auto row within the existing calendar card, preserving its brush and radius.
        for (auto childElement:host.Children()) {
            auto child=childElement.try_as<xaml::FrameworkElement>();
            if (!child) continue;
            auto oldRow=controls::Grid::GetRow(child);
            panel.originalRows.emplace_back(winrt::make_weak(child),oldRow);
            controls::Grid::SetRow(child,oldRow+1);
        }
        if (host.RowDefinitions().Size()==0) {
            controls::RowDefinition original;original.Height({1,xaml::GridUnitType::Star});
            host.RowDefinitions().Append(original);panel.madeOriginalRow=true;
        }
        panel.row=controls::RowDefinition();panel.row.Height({0,xaml::GridUnitType::Auto});
        host.RowDefinitions().InsertAt(0,panel.row);
        controls::Grid::SetRow(panel.content,0);host.Children().InsertAt(0,panel.content);
        Update(panel);
        Wh_Log(L"NCM: attached above calendar, rows=%u",host.RowDefinitions().Size());
    } catch (...) {
        Wh_Log(L"NCM: attach failed %08X",winrt::to_hresult());
        try {
            uint32_t index=0;
            if (panel.content && host.Children().IndexOf(panel.content,index)) host.Children().RemoveAt(index);
            for (auto const& [weak,row]:panel.originalRows) if (auto child=weak.get()) controls::Grid::SetRow(child,row);
            if (panel.row && host.RowDefinitions().IndexOf(panel.row,index)) host.RowDefinitions().RemoveAt(index);
            if (panel.madeOriginalRow && host.RowDefinitions().Size()==1) host.RowDefinitions().Clear();
        } catch (...) {}
        panel.content=nullptr;panel.row=nullptr;panel.originalRows.clear();panel.madeOriginalRow=false;
    }
    attaching=false;
}
void ElementAdded(xaml::FrameworkElement const& element) {
    if (attaching || element.Name()!=L"CalendarCenterGrid") return;
    auto host=element.try_as<controls::Grid>();if (!host) return;
    for (auto& panel:panels) if (panel.host.get()==host) return;
    Panel panel;panel.host=winrt::make_weak(host);panels.push_back(std::move(panel));
    if (!timer) {
        timer=xaml::DispatcherTimer();timer.Interval(std::chrono::seconds(1));
        tick=timer.Tick([](auto const&,auto const&) {
            for (auto it=panels.begin();it!=panels.end();) {
                if (!it->host.get()) {it=panels.erase(it);continue;}
                Attach(*it);Update(*it);++it;
            }
        });
        timer.Start();
    }
}
void DetachThread() {
    if (timer) {timer.Stop();timer.Tick(tick);timer=nullptr;tick={};}
    attaching=true;
    for (auto& panel:panels) try {
        auto host=panel.host.get();if (!host || !panel.content) continue;
        uint32_t index=0;
        if (host.Children().IndexOf(panel.content,index)) host.Children().RemoveAt(index);
        for (auto const& [weak,row]:panel.originalRows) if (auto child=weak.get()) controls::Grid::SetRow(child,row);
        if (panel.row && host.RowDefinitions().IndexOf(panel.row,index)) host.RowDefinitions().RemoveAt(index);
        if (panel.madeOriginalRow && host.RowDefinitions().Size()==1) host.RowDefinitions().Clear();
    } catch (...) {Wh_Log(L"NCM: detach failed %08X",winrt::to_hresult());}
    panels.clear();attaching=false;
}
} // namespace ncm
