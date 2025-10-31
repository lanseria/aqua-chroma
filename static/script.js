// static/script.js

document.addEventListener('DOMContentLoaded', () => {
    // --- DOM 元素获取 ---
    const container = document.getElementById('results-container');
    const statusDiv = document.getElementById('status');
    const loader = document.getElementById('loader');
    const chartDom = document.getElementById('echarts-container');

    // --- 状态管理 ---
    let allData = [];
    let currentIndex = 0;
    const itemsPerPage = 12;
    let isLoading = false;
    let myChart = echarts.init(chartDom, 'dark'); // 初始化 ECharts 实例

    // --- 辅助函数：根据百分比获取颜色 ---
    const getBluenessColor = (p) => {
        if (p < 20) return '#8c6b4f'; // 浑浊的棕色
        if (p < 50) return '#6495ED'; // 矢车菊蓝
        if (p < 80) return '#00BFFF'; // 深天蓝
        return '#1E90FF';   // 道奇蓝
    };

    const getCloudColor = (p) => {
        if (p < 20) return '#ADD8E6'; // 浅蓝色 (晴朗)
        if (p < 50) return '#D3D3D3'; // 浅灰色 (少云)
        if (p < 80) return '#A9A9A9'; // 深灰色 (多云)
        return '#696969';   // 暗灰色 (阴天)
    };

    // --- ECharts 渲染函数 ---
    const renderChart = (data) => {
        const chartData = data
            .filter(item => item.result && item.result['海蓝程度']) // 过滤掉无效数据
            .map(item => {
                const timestamp = new Date(item.timestamp * 1000);
                const blueness = parseFloat(item.result['海蓝程度']) || 0;
                return [timestamp, blueness];
            });

        const option = {
            backgroundColor: 'transparent',
            tooltip: { trigger: 'axis' },
            grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
            xAxis: { type: 'time', boundaryGap: false },
            yAxis: { type: 'value', min: 0, max: 100, axisLabel: { formatter: '{value} %' } },
            series: [{
                name: '海蓝程度',
                type: 'line',
                smooth: true,
                symbol: 'none',
                data: chartData,
                areaStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{
                        offset: 0, color: 'rgba(30, 144, 255, 0.5)'
                    }, {
                        offset: 1, color: 'rgba(30, 144, 255, 0)'
                    }])
                }
            }]
        };
        myChart.setOption(option);
    };

    // --- 卡片渲染函数 (已更新) ---
    const renderItems = () => {
        if (currentIndex >= allData.length) {
            loader.classList.add('hidden');
            return;
        }
        const itemsToRender = allData.slice(currentIndex, currentIndex + itemsPerPage);
        
        itemsToRender.forEach(item => {
            const result = item.result;
            const card = document.createElement('div');
            card.className = 'result-card';
            const formattedDate = new Date(item.timestamp * 1000).toLocaleString('zh-CN', { hour12: false });
            
            const bluenessPercent = parseFloat(result['海蓝程度']) || 0;
            const cloudPercent = parseFloat(result['云层覆盖率']) || 0;

            card.innerHTML = `
                <div class="card-header"><h2>${formattedDate}</h2></div>
                <div class="card-body">
                    <p>状态: <span class="value">${result.status}</span></p>
                    
                    <!-- 海蓝程度进度条 -->
                    <div class="progress-container">
                        <div class="progress-label">
                            <span>海蓝程度</span>
                            <span>${bluenessPercent.toFixed(2)}%</span>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-bar-fill" data-width="${bluenessPercent}%" style="background-color: ${getBluenessColor(bluenessPercent)};"></div>
                        </div>
                    </div>

                    <!-- 云层覆盖率进度条 -->
                    <div class="progress-container">
                        <div class="progress-label">
                            <span>云层覆盖率</span>
                            <span>${cloudPercent.toFixed(2)}%</span>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-bar-fill" data-width="${cloudPercent}%" style="background-color: ${getCloudColor(cloudPercent)};"></div>
                        </div>
                    </div>
                </div>
                <div class="image-gallery">
                    <figure>
                        <img src="/${item.output_directory}/03_ocean_only.png" alt="裁剪后图像" loading="lazy">
                        <figcaption>海洋区域</figcaption>
                    </figure>
                    <figure>
                        <img src="/${item.output_directory}/04_cloud_mask.png" alt="云层蒙版" loading="lazy">
                        <figcaption>云层蒙版</figcaption>
                    </figure>
                </div>
            `;
            container.appendChild(card);
        });

        // 延迟一小段时间再设置宽度，以触发CSS动画
        setTimeout(() => {
            const newFills = container.querySelectorAll('.progress-bar-fill:not(.animated)');
            newFills.forEach(fill => {
                fill.style.width = fill.getAttribute('data-width');
                fill.classList.add('animated');
            });
        }, 100);

        currentIndex += itemsPerPage;
    };

    // --- 滚动和初始化逻辑 (保持不变) ---
    const handleScroll = () => {
        if (isLoading || currentIndex >= allData.length) return;
        if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 300) {
            isLoading = true;
            loader.classList.remove('hidden');
            setTimeout(() => {
                renderItems();
                isLoading = false;
            }, 500);
        }
    };

    const init = async () => {
        try {
            const response = await fetch('/results');
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
            
            const data = await response.json();
            if (data.length === 0) {
                statusDiv.textContent = '暂无分析数据，请等待后台任务执行...';
                return;
            }

            // 【重要】先用原始顺序数据渲染图表
            renderChart(data);

            // 然后再倒序数据用于卡片展示
            allData = data.reverse();
            statusDiv.textContent = `共加载 ${allData.length} 条记录。向下滚动以查看更多。`;
            
            renderItems();
            window.addEventListener('scroll', handleScroll);

        } catch (error) {
            console.error("获取数据失败:", error);
            statusDiv.textContent = '获取数据失败，请检查后端服务是否正常。';
        }
    };

    // 监听窗口大小变化，使图表自适应
    window.addEventListener('resize', () => {
        myChart.resize();
    });

    // 启动应用
    init();
});